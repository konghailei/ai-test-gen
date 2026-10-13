"""Milvus 向量存储适配器。

通过 pymilvus 连接 Milvus Standalone，统一作为系统唯一向量库后端
（ChromaDB 已弃用）。配合 bge-large-zh-v1.5 嵌入模型，1024 维中文语义向量。

设计依据：
- docs/05-technical-selection.md §4（向量数据库选型）
- docs/05-technical-selection.md §5.1（bge-large-zh-v1.5 推荐）
- docs/02-knowledge-base.md §2（双引擎存储架构）
- docs/02-knowledge-base.md §9（大规模检索优化：HNSW + 分区）

Collection 设计（5 个，>10w 条优化策略）：
- aitestgen_requirements  — 需求向量（ID: requirement ID）
- aitestgen_test_cases    — 用例向量（ID: test case ID）
- aitestgen_patterns      — 测试模式向量（ID: pattern ID）
- aitestgen_req_pats      — 需求模式向量（ID: requirement pattern ID）
- aitestgen_errors        — 错误案例向量（ID: error case ID）

索引：HNSW（M=16, efConstruction=200, ef=64），适合 >10w 向量
分区：按模块分区（auth/payment/user/order/system/_default），检索时缩小扫描范围
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any

from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    MilvusClient,
    connections,
    utility,
)
from sentence_transformers import SentenceTransformer

from app.config import settings
from app.utils.logger import logger


# 每个 Collection 的维度和索引配置
# bge-large-zh-v1.5 输出 1024 维
# ---------------------------------------------------------------------------
# docs/27 分片并行（dev 机已定标探针；dev 不开 / 服务器自适应）
# ---------------------------------------------------------------------------
# 探针实证（2026-10-12，8 逻辑核 dev 容器，bge-large 48 条实测）：
#   - 串行 4 线程 127.2s；ThreadPool 2 worker 122.4s（1.04×，≈假并行）——
#     GIL 之外，torch intra-op 线程池进程级共享，worker 线程在同 4 个 OMP
#     线程上排队，**线程池形态永远无效**；
#   - ThreadPool 4 worker 333.2s（0.38×，负优化）——4×4=16 线程争 8 逻辑核；
#   - ProcessPool 在 dev 机因内存不足（宿主 16GB，模型 1.3GB/worker）无法
#     完成基准——**dev 机不开并行**，workers=1 即串行。
# 服务器形态：DEDUP_EMBED_WORKERS>1 时用 ProcessPoolExecutor 分片，每 worker
#   独立加载模型 + set_num_threads(4)（4 线程是单次 encode 实测最优粒度，
#   docs/26 线程定标）。进程池经 spawn 启动，模型只加载一次、后续复用。
# 内存预算：worker 数 × ~1.6GB（模型权重 + 运行时）。服务器至少留出
#   workers × 1.6GB 空闲内存再开。
# 判定零影响：分片只改"算得快不快"，每条文本的向量与串行逐位一致
#   （normalize 前 float32 推理，无量化）。
_DEDUP_PROC_POOL: "ProcessPoolExecutor | None" = None
_DEDUP_PROC_POOL_LOCK = threading.Lock()
_DEDUP_PROC_POOL_WORKERS = 0  # 当前池的 worker 数（与配置不符时重建）


def _dedup_encode_worker_init() -> None:
    """进程池 worker 初始化：固定 4 线程（探针实测最优）。"""
    import torch as _torch

    _torch.set_num_threads(4)
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def _dedup_encode_chunk(args: tuple[list[str], str]) -> list[list[float]]:
    """进程池 worker：加载（复用）本进程模型并 encode 一个分片。"""
    chunk, model_path = args
    global _DEDUP_WORKER_MODEL
    m = globals().get("_DEDUP_WORKER_MODEL")
    if m is None:
        from sentence_transformers import SentenceTransformer as _ST

        m = _ST(model_path, device="cpu")
        globals()["_DEDUP_WORKER_MODEL"] = m
    vecs = m.encode(chunk, normalize_embeddings=True, show_progress_bar=False)
    return vecs.tolist()


def _get_dedup_proc_pool(workers: int):
    """获取/复用进程池（worker 数变化时重建）。"""
    global _DEDUP_PROC_POOL, _DEDUP_PROC_POOL_WORKERS
    if workers <= 1:
        return None
    if _DEDUP_PROC_POOL is not None and _DEDUP_PROC_POOL_WORKERS == workers:
        return _DEDUP_PROC_POOL
    with _DEDUP_PROC_POOL_LOCK:
        if _DEDUP_PROC_POOL is not None and _DEDUP_PROC_POOL_WORKERS == workers:
            return _DEDUP_PROC_POOL
        if _DEDUP_PROC_POOL is not None:
            try:
                _DEDUP_PROC_POOL.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa: BLE001
                pass
            _DEDUP_PROC_POOL = None
        from concurrent.futures import ProcessPoolExecutor as _PPE

        _DEDUP_PROC_POOL = _PPE(
            max_workers=workers,
            initializer=_dedup_encode_worker_init,
        )
        _DEDUP_PROC_POOL_WORKERS = workers
        logger.info("[Milvus] 去重分片并行进程池就绪: {} workers", workers)
    return _DEDUP_PROC_POOL


def _physical_cores() -> int:
    """保守估物理核（逻辑核 ÷2 去超线程；容器内 cpu_count 含超线程）。"""
    try:
        return max(1, (os.cpu_count() or 2) // 2)
    except Exception:  # noqa: BLE001
        return 1


def _dedup_workers_configured() -> int:
    """解析 DEDUP_EMBED_WORKERS：auto = max(1, 物理核-1)，留 1 核给主进程。"""
    raw = str(settings.DEDUP_EMBED_WORKERS).strip().lower()
    if raw in ("", "1", "off", "0"):
        return 1
    if raw == "auto":
        return max(1, _physical_cores() - 1)
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning(
            "[Milvus] DEDUP_EMBED_WORKERS={} 无法解析，回退串行", raw,
        )
        return 1


def _encode_dedup_texts(
    texts: list[str],
    model_path: str | None,
    serial_encode: "callable",
) -> list[list[float]]:
    """去重 embedding 主入口：串行或分片并行（每 worker 4 线程）。

    workers=1（dev 默认）→ serial_encode(texts)，行为与 v3.5.8 前完全一致。
    workers>1 → ProcessPool 分片（worker 内独立加载 model_path）；
    任何并行异常回退串行重算（不击穿去重链路）。
    """
    workers = _dedup_workers_configured()
    if workers > 1 and len(texts) >= workers * 2 and model_path:
        pool = _get_dedup_proc_pool(workers)
        if pool is not None:
            try:
                chunks = [texts[i::workers] for i in range(workers)]
                futs = [
                    pool.submit(_dedup_encode_chunk, (c, model_path))
                    for c in chunks if c
                ]
                out: list[list[float]] = []
                for f in futs:
                    out.extend(f.result())
                return out
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "[Milvus] 分片并行失败，回退串行: {}", str(e)[:200],
                )
    return serial_encode(texts)


COLLECTION_CONFIGS = {
    "requirements": {
        "description": "需求向量",
        "dim": 1024,
    },
    "test_cases": {
        "description": "用例向量",
        "dim": 1024,
    },
    "patterns": {
        "description": "测试模式向量",
        "dim": 1024,
    },
    "req_pats": {
        "description": "需求模式向量",
        "dim": 1024,
    },
    "errors": {
        "description": "错误案例向量",
        "dim": 1024,
    },
}

# 按模块分区，检索时只扫描目标分区，延迟降低 5-10 倍（docs/02 §9.2）
MODULE_PARTITIONS = ["auth", "payment", "user", "order", "system", "_default"]

# 进入 Milvus 表达式的标识白名单（防止引号注入 / 表达式解析失败）
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _safe_id(value: str, field: str) -> str:
    """校验进入 Milvus 表达式的标识，防止引号注入 / 表达式解析失败。

    仅允许字母、数字、下划线、连字符。product_id / req_id / case_id 等
    均由 id_gen 生成（格式如 PROD-xxx / REQ-xxx / TC-xxx），符合该规则。
    """
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValueError(
            f"非法的 {field} 标识: {value!r}（仅允许字母、数字、_ 和 -）"
        )
    return value


class MilvusVectorStore:
    """Milvus 向量存储（系统唯一向量库后端）。"""

    def __init__(self):
        self._model: SentenceTransformer | None = None
        self._connected = False
        self._model_error = ""
        self._connect_lock = threading.Lock()
        self._model_lock = threading.Lock()
        # docs/27 A：去重专用模型槽——与全局 _model 完全隔离（独立锁/独立加载），
        # KB 检索与 Milvus 向量化绝不使用它；反之去重也不写 self._model。
        self._dedup_model: SentenceTransformer | None = None
        self._dedup_model_lock = threading.Lock()
        self._dedup_model_broken = False

    @property
    def test_patterns(self):
        """语义检索预热时访问 patterns Collection 的属性。"""
        return self._get_collection("patterns")

    # ========================================================================
    # 初始化
    # ========================================================================

    def _ensure_connected(self) -> None:
        """确保已连接 Milvus（线程安全，防止并发重复连接）。"""
        if self._connected:
            return
        with self._connect_lock:
            if self._connected:
                return
            host = settings.MILVUS_HOST
            port = settings.MILVUS_PORT
            logger.info("[Milvus] 连接 Milvus: {}:{}", host, port)
            connections.connect("default", host=host, port=port)
            self._connected = True
            self._ensure_collections()

    @staticmethod
    def _model_dir_valid(model_dir: str) -> bool:
        """判断本地模型目录是否包含可加载的权重文件。

        需同时具备 config.json 与至少一个权重文件（兼容 pytorch_model.bin / model.safetensors）。
        """
        if not os.path.isdir(model_dir):
            return False
        if not os.path.isfile(os.path.join(model_dir, "config.json")):
            return False
        weights = ("pytorch_model.bin", "model.safetensors", "pytorch_model.bin.index.json", "model.safetensors.index.json")
        return any(os.path.isfile(os.path.join(model_dir, w)) for w in weights)

    def _ensure_model(self) -> SentenceTransformer:
        """确保嵌入模型已加载（线程安全，避免并发重复加载）。

        加载优先级：
        1) EMBEDDING_MODEL_NAME 为绝对路径 → 视为本地模型目录（基础镜像内置或宿主机 bind mount）。
           若目录不完整，**明确报错**（不再误走 HuggingFace 下载，避免网络不通时给出混淆报错）。
        2) 兼容旧版缓存目录结构（早期预下载遗留）。
        3) 否则按 HuggingFace 仓库名联网下载。
        """
        if self._model is None:
            with self._model_lock:
                if self._model is None:
                    model_name = settings.EMBEDDING_MODEL_NAME
                    device = settings.EMBEDDING_DEVICE
                    self._model_error = ""

                    # 1) 绝对路径 → 本地模型目录
                    if os.path.isabs(model_name):
                        if self._model_dir_valid(model_name):
                            logger.info("[Milvus] 加载本地模型: {} (device={})", model_name, device)
                            self._model = SentenceTransformer(model_name, device=device)
                            return self._model
                        # 本地目录缺失/不完整 → 明确报错，不回退到联网下载
                        self._model_error = (
                            f"本地嵌入模型未就绪：未在 {model_name} 找到完整的模型文件"
                            f"（需 config.json + pytorch_model.bin / model.safetensors）。"
                            f"请确认该目录已挂载到容器且权重文件已下载；"
                            f"若模型目录是容器启动后新增的，请重启后端与 celery_worker 以重新建立 bind mount。"
                        )
                        logger.error("[Milvus] {}", self._model_error)
                        raise FileNotFoundError(self._model_error)

                    # 2) 兼容旧版缓存目录结构（早期 modelscope 预下载遗留）
                    local_paths = [
                        "/root/.cache/huggingface/models/BAAI--bge-large-zh-v1.5/snapshots/master",
                        "/root/.cache/huggingface/hub/models--BAAI--bge-large-zh-v1.5/snapshots/main",
                    ]
                    local_model = None
                    for p in local_paths:
                        if self._model_dir_valid(p):
                            local_model = p
                            break

                    if local_model:
                        logger.info("[Milvus] 加载预置模型: {} (device={})", local_model, device)
                        self._model = SentenceTransformer(local_model, device=device)
                        return self._model

                    # 3) HuggingFace 仓库名 → 联网下载
                    logger.info("[Milvus] 下载模型: {} (device={})", model_name, device)
                    self._model = SentenceTransformer(model_name, device=device)
        return self._model

    def model_status(self) -> dict:
        """返回嵌入模型就绪状态与诊断信息（供 /knowledge/model-status 使用）。

        与 is_model_ready() 不同，此处会捕获并返回具体错误，便于前端展示“为什么未就绪”。
        """
        try:
            self._ensure_model()
            return {
                "ready": True,
                "path": settings.EMBEDDING_MODEL_NAME,
                "device": settings.EMBEDDING_DEVICE,
                "error": "",
            }
        except Exception as e:
            return {
                "ready": False,
                "path": settings.EMBEDDING_MODEL_NAME,
                "device": settings.EMBEDDING_DEVICE,
                "error": str(e)[:300],
            }

    def _collection_name(self, name: str) -> str:
        """生成带前缀的 Collection 名称。"""
        prefix = settings.MILVUS_COLLECTION_PREFIX
        return f"{prefix}_{name}"

    def _ensure_collections(self) -> None:
        """确保所有 Collection 已创建（含分区）。"""
        for coll_key, cfg in COLLECTION_CONFIGS.items():
            coll_name = self._collection_name(coll_key)
            if not utility.has_collection(coll_name):
                self._create_collection(coll_name, cfg["dim"])
                self._create_partitions(coll_name)
                logger.info("[Milvus] 创建 Collection + 分区: {}", coll_name)
            else:
                self._migrate_index_if_needed(coll_name)
                self._create_partitions(coll_name)

    def _create_collection(self, coll_name: str, dim: int) -> None:
        """创建 Collection（id + vector + metadata + document）。"""
        fields = [
            FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=256),
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=dim),
            FieldSchema(name="metadata", dtype=DataType.JSON),
            FieldSchema(name="document", dtype=DataType.VARCHAR, max_length=65535),
        ]
        schema = CollectionSchema(fields, description=coll_name)

        # HNSW 索引（适合 >10w 向量，docs/02 §9.3）
        index_params = {
            "metric_type": "COSINE",
            "index_type": "HNSW",
            "params": {"M": 16, "efConstruction": 200},
        }
        collection = Collection(coll_name, schema)
        collection.create_index("vector", index_params)
        collection.load()

    def _create_partitions(self, coll_name: str) -> None:
        """为 Collection 创建模块分区（docs/02 §9.2）。"""
        collection = Collection(coll_name)
        existing_partitions = [p.name for p in collection.partitions]
        for partition_name in MODULE_PARTITIONS:
            if partition_name not in existing_partitions:
                try:
                    collection.create_partition(partition_name)
                except Exception as e:
                    logger.warning("[Milvus] 创建分区失败 partition={}: {}", partition_name, str(e)[:150])

    def _get_partition_name(self, metadata: dict) -> str:
        """根据 metadata 中的模块信息选择分区名。"""
        modules = metadata.get("modules", "")
        if isinstance(modules, str) and modules:
            first_module = modules.split(",")[0].strip().lower()
            if first_module in MODULE_PARTITIONS:
                return first_module
        return "_default"

    def _migrate_index_if_needed(self, coll_name: str) -> None:
        """对已存在的 Collection 检查索引类型，若为 IVF_FLAT 则重建为 HNSW。"""
        try:
            collection = Collection(coll_name)
            indexes = collection.indexes
            for idx in indexes:
                idx_info = idx.params
                if idx_info.get("index_type") == "IVF_FLAT":
                    logger.info("[Milvus] 索引迁移: {} IVF_FLAT → HNSW", coll_name)
                    collection.release()
                    collection.drop_index()
                    new_index_params = {
                        "metric_type": "COSINE",
                        "index_type": "HNSW",
                        "params": {"M": 16, "efConstruction": 200},
                    }
                    collection.create_index("vector", new_index_params)
                    collection.load()
                    logger.info("[Milvus] 索引迁移完成: {}", coll_name)
                    break
        except Exception as e:
            logger.warning("[Milvus] 索引迁移检查失败: {} error={}", coll_name, str(e)[:200])

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """批量文本 → 向量。"""
        model = self._ensure_model()
        embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return embeddings.tolist()

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量文本 → 向量（供语义去重 / 一致性相似度比对等同步场景调用）。"""
        if not texts:
            return []
        return self._embed(texts)

    # ------------------------------------------------------------------
    # docs/27 A：去重专用嵌入模型（与全局模型完全解耦）
    # ------------------------------------------------------------------

    def _ensure_dedup_model(self) -> SentenceTransformer | None:
        """加载去重专用模型（docs/27 A）。

        隔离原则：独立槽位/独立锁，**绝不写 self._model**——全局模型服务于
        KB 检索与 Milvus 向量化（1024 维），去重可用轻量模型（bge-small
        512 维），两者向量空间不同，混用即错。

        加载规则（与 _ensure_model 的本地目录分支对齐）：
        - DEDUP_EMBEDDING_MODEL_NAME 为空 → None（调用方回退全局模型）；
        - 绝对路径且目录有效 → 加载（双检锁，防并发重复加载）；
        - 目录无效 → 明确报错（不回退联网下载，与全局路径同哲学）；
        - 任何加载异常由调用方接住并回退全局模型（性能特性，不阻断生成）。
        """
        name = settings.DEDUP_EMBEDDING_MODEL_NAME
        if not name:
            return None
        if self._dedup_model is None:
            with self._dedup_model_lock:
                if self._dedup_model is None:
                    if not os.path.isabs(name) or not self._model_dir_valid(name):
                        msg = (
                            f"去重专用模型未就绪：DEDUP_EMBEDDING_MODEL_NAME={name}"
                            f"（需为绝对路径且含 config.json + 权重文件）"
                        )
                        logger.error("[Milvus] {}", msg)
                        raise FileNotFoundError(msg)
                    logger.info("[Milvus] 加载去重专用模型: {} (device={})",
                                name, settings.DEDUP_EMBEDDING_DEVICE)
                    self._dedup_model = SentenceTransformer(
                        name, device=settings.DEDUP_EMBEDDING_DEVICE
                    )
        return self._dedup_model

    def dedup_model_id(self) -> str:
        """当前生效的去重 embed 模型标识（进 B 缓存 key，docs/27 §2.5）。

        空配置 → "global"；配置了但加载失败（回退全局）→ "global-fallback"
        ——保证缓存 key 反映**实际产出向量的模型**，防模型错位污染。
        """
        if not settings.DEDUP_EMBEDDING_MODEL_NAME:
            return "global"
        if self._dedup_model_broken:
            return "global-fallback"
        return settings.DEDUP_EMBEDDING_MODEL_NAME

    def embed_texts_for_dedup(self, texts: list[str]) -> list[list[float]]:
        """去重专用 embed 入口（docs/27 A；orchestrator 两处 dedup_semantic 用）。

        模型链：DEDUP_EMBEDDING_MODEL_NAME 有效 → 专用模型；
        为空/加载失败 → 全局 _embed（log warning）。
        v3.5.9（docs/27 分片并行）：入口改经 _encode_dedup_texts——
        workers=1 时与原行为逐位一致；workers>1 时 ProcessPool 分片
        （worker 内独立加载模型，与主进程模型解耦）。
        调用方（dedup_semantic）对 embed 异常已有 3-gram 降级接住。
        """
        if not texts:
            return []

        def _serial(ts: list[str]) -> list[list[float]]:
            try:
                model = self._ensure_dedup_model()
            except Exception as e:  # noqa: BLE001
                self._dedup_model_broken = True
                logger.warning(
                    "[Milvus] 去重专用模型加载失败，回退全局模型: {}", str(e)[:200]
                )
                model = None
            if model is None:
                return self._embed(ts)
            vectors = model.encode(
                ts, normalize_embeddings=True, show_progress_bar=False
            )
            return vectors.tolist()

        # 并行 worker 需要独立的模型路径；专用模型配置了但加载失败（broken）
        # 时无路径可用 → 直接串行（内部会回退全局模型）。
        model_path = None
        if not self._dedup_model_broken:
            name = settings.DEDUP_EMBEDDING_MODEL_NAME
            if name and os.path.isabs(name) and self._model_dir_valid(name):
                model_path = name
            else:
                # 全局模型路径（容器内挂载形态，compose: /models/bge-large-zh-v1.5）
                gm = settings.EMBEDDING_MODEL_NAME
                if gm and os.path.isabs(gm) and self._model_dir_valid(gm):
                    model_path = gm
        return _encode_dedup_texts(texts, model_path, _serial)

    def _get_collection(self, coll_key: str) -> Collection:
        """获取已加载的 Collection。"""
        self._ensure_connected()
        coll_name = self._collection_name(coll_key)
        collection = Collection(coll_name)
        collection.load()
        return collection

    def _upsert(self, coll_key: str, ids: list[str], docs: list[str], metadatas: list[dict]) -> None:
        """写入/更新向量（按模块分区写入）。"""
        if not ids:
            return
        collection = self._get_collection(coll_key)
        vectors = self._embed(docs)

        # Upsert：先删后插。Milvus expr 字符串字面量须用双引号；
        # repr() 生成单引号会导致解析失败，故用 json.dumps 生成双引号字面量。
        collection.delete(f'id in [{",".join(json.dumps(i, ensure_ascii=False) for i in ids)}]')
        # metadata 列为 JSON 类型，必须直接传入 dict——若传入 json.dumps 字符串，
        # Milvus 会将其存储为 JSON 字符串值（而非 JSON 对象），
        # 导致 metadata["product_id"] 等表达式过滤失效（始终返回 0 条）。
        # 按模块分区写入（docs/02 §9.2），检索时只扫描目标分区
        partition_name = self._get_partition_name(metadatas[0]) if metadatas else "_default"
        collection.insert([
            ids,
            vectors,
            metadatas,
            docs,
        ], partition_name=partition_name)
        collection.flush()

    def _search(
        self,
        coll_key: str,
        query: str,
        top_k: int = 5,
        where_filter: str | None = None,
    ) -> list[dict]:
        """向量检索。

        Args:
            coll_key: Collection key（requirements/test_cases/patterns/errors）
            query: 查询文本
            top_k: 返回 Top-K
            where_filter: 标量过滤表达式（Milvus expr 语法），如 'status == "archived"'。
                          传入的标识已在调用方经 _safe_id 校验。

        Returns:
            list[dict]: 标准化结果，含 id / similarity / metadata / document
        """
        collection = self._get_collection(coll_key)
        query_vector = self._embed([query])[0]

        # HNSW 检索参数（docs/02 §9.3）
        search_params = {"metric_type": "COSINE", "params": {"ef": 64}}

        results = collection.search(
            data=[query_vector],
            anns_field="vector",
            param=search_params,
            limit=top_k,
            expr=where_filter,
            output_fields=["id", "metadata", "document"],
        )

        formatted = []
        for hits in results:
            for hit in hits:
                # pymilvus Entity.get() 在某些版本不支持默认值参数，
                # 使用 try/except 安全访问各字段。
                meta_raw = "{}"
                doc_raw = ""
                try:
                    meta_raw = hit.entity.get("metadata")
                except Exception as e:
                    logger.warning("[Milvus] 读取 metadata 字段失败: {}", str(e)[:150])
                try:
                    doc_raw = hit.entity.get("document")
                except Exception as e:
                    logger.warning("[Milvus] 读取 document 字段失败: {}", str(e)[:150])
                if isinstance(meta_raw, str):
                    meta_raw = json.loads(meta_raw)
                formatted.append({
                    "id": hit.id,
                    "similarity": round(hit.distance, 4),
                    "metadata": meta_raw or {},
                    "document": doc_raw or "",
                })
        return formatted

    # ========================================================================
    # 需求向量操作（兼容原 VectorStore 接口）
    # ========================================================================

    def add_requirement(self, req_id: str, srr_json: dict, metadata: dict | None = None) -> None:
        """添加需求到向量库。

        v2.34: 支持 type 字段区分总 SRR 与功能点 SRR。
            - 调用方未传 type 时默认 "requirement"（总 SRR，向后兼容）
            - 调用方传 type="functional_point" 时保留，用于归档 FP 时区分文档类型
        """
        self._ensure_connected()
        text = self._srr_to_text(srr_json)
        meta = metadata or {}
        meta["req_id"] = req_id
        # v2.34: doc_type 与 type 保持一致（FP 归档时 type="functional_point"）
        # 保留向后兼容：未显式指定 type 时默认 "requirement"
        if "type" not in meta:
            meta["type"] = "requirement"
        meta["doc_type"] = meta["type"]
        # 产品隔离：优先使用传入 metadata 的 product_id，否则从 SRR 提取；兜底 PROD-DEFAULT。
        # 保证无论调用方是否显式传入，向量库 metadata 都带 product_id，
        # 使 search/get_all/count 的 product_id 过滤一致生效。
        meta["product_id"] = (
            meta.get("product_id")
            or (srr_json.get("requirement", {}) or {}).get("product_id", "")
            or "PROD-DEFAULT"
        )
        # v2.45: 项目隔离兜底——保证按项目删除/检索向量时 metadata 带 project_id。
        # 调用方（如 archive_project）已显式传入 project_id 时优先使用；
        # 否则从 SRR 的 requirement.project_id 提取；缺失时为空字符串。
        meta["project_id"] = (
            meta.get("project_id")
            or (srr_json.get("requirement", {}) or {}).get("project_id", "")
            or ""
        )
        # srr_json 作为 metadata 中的字符串字段存储（JSON 编码后的完整 SRR），
        # 供 ClarifyAgent KB 自动补全经 meta.get("srr_json") + json.loads 提取字段值。
        # Milvus metadata 上限 65536 字节 → 超大文档需裁剪（去掉 _raw_text/add_details 等长字段）。
        srr_for_meta = self._srr_for_metadata(srr_json)
        meta["srr_json"] = json.dumps(srr_for_meta, ensure_ascii=False)
        self._upsert("requirements", [req_id], [text], [meta])
        # v3.4.1 修复 M-2：写入后失效该产品的 KB 检索缓存（24h TTL），
        # 否则新归档/更新的需求在已有缓存命中的 query 上"搜不到"。
        # 放在此处可覆盖全部写入路径（outbox relay / Celery worker / 同步降级）。
        try:
            from app.db.kb_cache import kb_cache
            kb_cache.invalidate_by_product_sync(meta.get("product_id") or "PROD-DEFAULT")
        except Exception as e:
            logger.debug("[Milvus] 失效 KB 缓存失败（不阻断写入）: {}", str(e)[:100])

    def search_similar_requirements(
        self,
        query: str,
        product_id: str | None = None,
        modules: list[str] | None = None,
        top_k: int = 5,
        status: str | None = None,
        doc_type: str | None = None,
        fp_id: str | None = None,
        requirement_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict]:
        """检索相似需求。

        v2.34 扩展：
            - 新增 doc_type 过滤（"requirement" / "functional_point"），实现总 SRR 与 FP 隔离
            - 新增 fp_id 过滤（按功能点 ID 过滤，仅对 FP 记录生效）
            - 新增 requirement_id 过滤（按需求 ID 过滤，可同时匹配总 SRR 和该需求下的 FP）
        v2.45 扩展：
            - 新增 project_id 过滤（按项目过滤检索结果）

        Args:
            query: 查询文本
            product_id: 按产品过滤
            modules: 按模块过滤（同时作用于总 SRR 的 modules 和 FP 的 modules）
            top_k: Top-K
            status: 按状态过滤（archived/draft）
            doc_type: 按文档类型过滤（v2.34 新增）
                - "requirement": 仅检索总 SRR 记录
                - "functional_point": 仅检索功能点 SRR 记录
                - None: 同时检索两类（默认，向后兼容）
            fp_id: 按功能点 ID 过滤（v2.34 新增，仅对 FP 记录生效）
            requirement_id: 按需求 ID 过滤（v2.34 新增）
            project_id: 按项目 ID 过滤（v2.45 新增）

        Returns:
            list[dict]: 相似需求列表
        """
        self._ensure_connected()
        # 构建 Milvus 表达式（标识经 _safe_id 校验，防止注入）
        conditions = []
        if product_id:
            conditions.append(f'metadata["product_id"] == "{_safe_id(product_id, "product_id")}"')
        if status:
            conditions.append(f'metadata["status"] == "{_safe_id(status, "status")}"')
        if doc_type:
            conditions.append(f'metadata["type"] == "{_safe_id(doc_type, "type")}"')
        if requirement_id:
            # v2.8.51 修复：总 SRR 写入 metadata.req_id，FP 归档写入 metadata.requirement_id
            # 需同时匹配两个字段，否则总 SRR 永远无法被 requirement_id 过滤命中
            safe_rid = _safe_id(requirement_id, "requirement_id")
            conditions.append(
                f'(metadata["requirement_id"] == "{safe_rid}" || metadata["req_id"] == "{safe_rid}")'
            )
        if project_id:
            conditions.append(f'metadata["project_id"] == "{_safe_id(project_id, "project_id")}"')
        where_filter = " && ".join(conditions) if conditions else None

        # modules 无法用 Milvus expr 直接过滤（以逗号分隔字符串存储于 metadata），
        # 放大召回量后在内存中按模块过滤，保持行为一致。
        # fp_id 同理（FP 记录的 metadata.fp_id 不能直接用 Milvus expr 过滤）。
        need_mem_filter = bool(modules) or bool(fp_id)
        fetch_k = top_k * 5 if need_mem_filter else top_k
        formatted = self._search("requirements", query, top_k=fetch_k, where_filter=where_filter)

        if need_mem_filter:
            wanted_modules = {m for m in modules} if modules else None
            filtered = []
            for r in formatted:
                meta = r.get("metadata", {}) or {}
                # 模块过滤
                if wanted_modules is not None:
                    raw = meta.get("modules", "")
                    stored = raw.split(",") if isinstance(raw, str) and raw else (raw or [])
                    stored_set = {m.strip() for m in stored if isinstance(m, str) and m.strip()}
                    if not (wanted_modules & stored_set):
                        continue
                # 功能点 ID 过滤
                if fp_id:
                    rec_fp_id = meta.get("fp_id", "")
                    if rec_fp_id != fp_id:
                        continue
                filtered.append(r)
            formatted = filtered[:top_k]
        return formatted

    def get_all_requirements(self, product_id: str | None = None, limit: int = 100) -> list[dict]:
        """获取所有需求向量（product_id 为空时不过滤，避免空表达式报错）。"""
        self._ensure_connected()
        collection = self._get_collection("requirements")
        # Milvus query 要求 expr 为 string 类型；无过滤条件时传空字符串
        expr = f'metadata["product_id"] == "{_safe_id(product_id, "product_id")}"' if product_id else ""
        results = collection.query(expr=expr, output_fields=["id", "metadata", "document"], limit=limit)
        parsed = []
        for r in results:
            meta = r.get("metadata", {})
            if isinstance(meta, str):
                meta = json.loads(meta)
            parsed.append({"id": r["id"], "metadata": meta or {}, "document": r.get("document", "")})
        return parsed

    def delete_requirement(self, req_id: str, product_id: str | None = None) -> None:
        """删除需求向量。

        v2.8.51 修复：必须 flush 才能让 delete 在 query/num_entities 中生效，
        否则 BOUNDED 一致性下查询仍会返回已删除的记录（导致删除产品后向量遗留）。

        v3.4.1 修复 M-2：新增可选 product_id 参数——传入时同步失效该产品的 KB
        检索缓存，避免已删除的需求在 24h TTL 内仍被检索到（"删了还在"）。
        """
        self._ensure_connected()
        collection = self._get_collection("requirements")
        collection.delete(f'id == "{_safe_id(req_id, "req_id")}"')
        collection.flush()
        if product_id:
            try:
                from app.db.kb_cache import kb_cache
                kb_cache.invalidate_by_product_sync(product_id)
            except Exception as e:
                logger.debug("[Milvus] 失效 KB 缓存失败（不阻断删除）: {}", str(e)[:100])

    def delete_requirement_from_kb(self, req_id: str, product_id: str | None = None) -> None:
        """从知识库删除需求（与 delete_requirement 同）。"""
        self.delete_requirement(req_id, product_id=product_id)

    def _delete_by_expr_paged(self, collection, expr: str, log_desc: str) -> int:
        """按表达式循环「查询 → 删除」，直到查空为止。

        v3.4.1 修复 L-6：Milvus 单次 query 有 16384 条上限。原实现各处都是
        `query(limit=16384)` → 一次 delete → `return len(ids)`，把"本次查到的条数"
        当作"已全部删除"返回，超出上限的部分被静默漏删（无异常、无日志告警）。

        Args:
            collection: Milvus collection 对象
            expr: 过滤表达式（调用方需自行做 ID 字面量安全处理）
            log_desc: 日志描述

        Returns:
            int: 实际删除的向量总数
        """
        total = 0
        # v3.4.4（自审加固）：轮次上限。原 while True 在 Milvus BOUNDED 一致性出现延迟时
        # （delete+flush 后 query 仍返回同批 id）会死循环；16 轮 × 16384 ≈ 26 万条已远超
        # 单产品数据量，触顶即告警并返回，交由人工核查。
        max_rounds = 16
        for _round in range(1, max_rounds + 1):
            results = collection.query(expr=expr, output_fields=["id"], limit=16384)
            if not results:
                return total
            ids = [r["id"] for r in results]
            # Milvus expr 字面量须用双引号
            ids_expr = ",".join(json.dumps(i, ensure_ascii=False) for i in ids)
            collection.delete(f"id in [{ids_expr}]")
            collection.flush()
            total += len(ids)
            logger.info("[Milvus] {} 分批删除: 第 {}/{} 轮，本批 {} 条，累计 {} 条",
                        log_desc, _round, max_rounds, len(ids), total)
        logger.error(
            "[Milvus] {} 分页删除达到轮次上限 {}（疑似一致性延迟或数据量异常），累计删除 {} 条，请人工核查剩余数据",
            log_desc, max_rounds, total,
        )
        return total

    def delete_requirement_by_product(self, product_id: str) -> int:
        """按 product_id 批量删除需求向量。

        用于删除产品时一次性清理该产品所有需求向量，比循环单条删除更高效。
        返回删除的记录数（基于查询统计，非 Milvus 内部删除标记数）。

        Args:
            product_id: 产品 ID

        Returns:
            实际删除的向量数
        """
        self._ensure_connected()
        collection = self._get_collection("requirements")
        safe_pid = _safe_id(product_id, "product_id")
        # v3.4.1 修复 L-6：改为循环分批删除，规避单次 query 16384 上限造成的静默漏删
        count = self._delete_by_expr_paged(
            collection,
            f'metadata["product_id"] == "{safe_pid}"',
            f"按 product_id 删除需求向量(product={product_id})",
        )
        # v3.4.4（自审补漏 M-2）：批量删除该产品全部需求向量后失效 KB 检索缓存——
        # 与 add_requirement 的失效对称。此前只有单条 delete_requirement(product_id) 失效，
        # 而删除产品/项目走本函数批量清理，导致 24h TTL 内"已删除产品的需求仍能被检索到"。
        try:
            from app.db.kb_cache import kb_cache
            kb_cache.invalidate_by_product_sync(product_id)
        except Exception as e:
            logger.debug("[Milvus] 失效 KB 缓存失败（不阻断删除）: {}", str(e)[:100])
        logger.info("[Milvus] 按 product_id 批量删除需求向量: product={}, count={}", product_id, count)
        return count

    def delete_requirement_by_project(self, project_id: str) -> int:
        """按 project_id 批量删除需求向量（v2.45 新增）。

        用于项目恢复活动时一次性清理该项目所有需求+FP 向量。

        Args:
            project_id: 项目 ID

        Returns:
            实际删除的向量数
        """
        self._ensure_connected()
        collection = self._get_collection("requirements")
        safe_pid = _safe_id(project_id, "project_id")
        # v3.4.1 修复 L-6：改为循环分批删除
        count = self._delete_by_expr_paged(
            collection,
            f'metadata["project_id"] == "{safe_pid}"',
            f"按 project_id 删除需求向量(project={project_id})",
        )
        logger.info("[Milvus] 按 project_id 批量删除需求向量: project={}, count={}", project_id, count)
        return count

    def update_requirement_metadata(self, req_id: str, metadata_updates: dict) -> None:
        """更新需求向量的元数据（如 go_live_date 变更）。

        Milvus 不支持原地更新 metadata，需查询现有记录 → 合并新 metadata → 重新 upsert。
        会重新计算 embedding，但操作频率低（仅修改上线时间时触发），可接受。
        """
        self._ensure_connected()
        collection = self._get_collection("requirements")
        results = collection.query(
            expr=f'id == "{_safe_id(req_id, "req_id")}"',
            output_fields=["id", "metadata", "document"],
            limit=1,
        )
        if not results:
            logger.warning("[Milvus] 更新元数据失败: 需求 {} 不在向量库中", req_id)
            return

        record = results[0]
        existing_meta = record.get("metadata", {})
        if isinstance(existing_meta, str):
            existing_meta = json.loads(existing_meta)
        existing_meta = existing_meta or {}
        existing_meta.update(metadata_updates)

        doc = record.get("document", "")
        self._upsert("requirements", [req_id], [doc], [existing_meta])
        logger.info("[Milvus] 需求 {} 元数据已更新: {}", req_id, list(metadata_updates.keys()))

    def requirement_exists(self, req_id: str) -> bool:
        """检查指定需求是否已存在于向量库。"""
        self._ensure_connected()
        collection = self._get_collection("requirements")
        results = collection.query(
            expr=f'id == "{_safe_id(req_id, "req_id")}"',
            output_fields=["id"],
            limit=1,
        )
        return len(results) > 0

    def get_collection_stats(self) -> dict:
        """获取所有 Collection 的统计信息（num_entities + 描述）。"""
        self._ensure_connected()
        stats: dict[str, dict] = {}
        for coll_key, cfg in COLLECTION_CONFIGS.items():
            try:
                collection = self._get_collection(coll_key)
                stats[coll_key] = {
                    "num_entities": collection.num_entities,
                    "description": cfg["description"],
                }
            except Exception as e:
                stats[coll_key] = {"num_entities": -1, "error": str(e)[:100]}
        return stats

    # ========================================================================
    # 用例向量操作
    # ========================================================================

    def add_test_case(self, case_id: str, case_json: dict, metadata: dict | None = None,
                      product_id: str | None = None) -> None:
        """添加用例到向量库。

        Args:
            product_id: 产品 ID（显式传入以强制产品隔离；缺省时回退 metadata 中的 product_id）
        """
        self._ensure_connected()
        text = json.dumps(case_json, ensure_ascii=False)
        meta = metadata or {}
        meta["case_id"] = case_id
        meta["doc_type"] = "test_case"
        # 产品隔离：显式 product_id 优先，否则取 metadata 中的；兜底 PROD-DEFAULT
        meta["product_id"] = product_id or meta.get("product_id", "") or "PROD-DEFAULT"
        # v2.45: 项目隔离兜底——保证按项目删除用例向量时 metadata 带 project_id
        meta["project_id"] = meta.get("project_id", "") or ""
        self._upsert("test_cases", [case_id], [text], [meta])

    def search_similar_cases(self, query: str, top_k: int = 5,
                             case_type: str | None = None,
                             req_id: str | None = None,
                             product_id: str | None = None,
                             project_id: str | None = None,
                             modules: list[str] | None = None) -> list[dict]:
        """检索相似用例。

        Args:
            query: 查询文本
            top_k: Top-K
            case_type: 按用例类型过滤（可选）
            req_id: 按需求 ID 过滤（可选）
            product_id: 按产品过滤（可选，产品隔离必须）
            project_id: 按项目过滤（v2.45 新增，与 search_similar_requirements 保持一致）
            modules: 按模块过滤（多选，内存过滤）
        """
        self._ensure_connected()
        conditions = []
        if case_type:
            conditions.append(f'metadata["case_type"] == "{_safe_id(case_type, "case_type")}"')
        if req_id:
            conditions.append(f'metadata["req_id"] == "{_safe_id(req_id, "req_id")}"')
        if product_id:
            conditions.append(f'metadata["product_id"] == "{_safe_id(product_id, "product_id")}"')
        if project_id:
            conditions.append(f'metadata["project_id"] == "{_safe_id(project_id, "project_id")}"')
        where_filter = " && ".join(conditions) if conditions else None

        # modules 无法用 Milvus expr 直接过滤（以逗号分隔字符串存储于 metadata），
        # 放大召回量后在内存中按模块过滤，保持与 search_similar_requirements 一致。
        fetch_k = top_k * 5 if modules else top_k
        formatted = self._search("test_cases", query, top_k=fetch_k, where_filter=where_filter)

        if modules:
            wanted = {m for m in modules}
            filtered = []
            for r in formatted:
                raw = r.get("metadata", {}).get("modules", "")
                stored = raw.split(",") if isinstance(raw, str) and raw else (raw or [])
                stored_set = {m.strip() for m in stored if isinstance(m, str) and m.strip()}
                if wanted & stored_set:
                    filtered.append(r)
            formatted = filtered[:top_k]
        return formatted

    def search_similar_cases_batch(
        self,
        queries: list[str],
        top_k: int = 5,
        case_type: str | None = None,
        req_id: str | None = None,
        product_id: str | None = None,
        project_id: str | None = None,
    ) -> list[list[dict]]:
        """批量检索相似用例（方案 C：embedding 一次 + 多向量 search 一次）。

        与 search_similar_cases 等效，但把 N 条用例的「N 次 embed + N 次 Milvus
        search RPC」收敛为「1 次批量 embed（embed_texts）+ 1 次多向量 search」。

        pymilvus 2.4.x 的 collection.search(data=[v1, v2, ...]) 支持多 query 向量，
        返回的 results 与 queries 等长对齐（results[i] 对应 queries[i] 的命中列表）。

        Args:
            queries: 查询文本列表（每条用例一个）
            top_k / case_type / req_id / product_id / project_id：同 search_similar_cases

        Returns:
            list[list[dict]]：每个 query 对应的标准化命中列表，顺序与 queries 对齐。
        """
        if not queries:
            return []
        self._ensure_connected()
        conditions = []
        if case_type:
            conditions.append(f'metadata["case_type"] == "{_safe_id(case_type, "case_type")}"')
        if req_id:
            conditions.append(f'metadata["req_id"] == "{_safe_id(req_id, "req_id")}"')
        if product_id:
            conditions.append(f'metadata["product_id"] == "{_safe_id(product_id, "product_id")}"')
        if project_id:
            conditions.append(f'metadata["project_id"] == "{_safe_id(project_id, "project_id")}"')
        where_filter = " && ".join(conditions) if conditions else None

        # 方案 C：一次批量 embedding（N 条文本 → N 个向量），避免每条一次 embed 调用
        vectors = self._embed(queries)

        collection = self._get_collection("test_cases")
        # HNSW 检索参数（docs/02 §9.3）
        search_params = {"metric_type": "COSINE", "params": {"ef": 64}}
        results = collection.search(
            data=vectors,
            anns_field="vector",
            param=search_params,
            limit=top_k,
            expr=where_filter,
            output_fields=["id", "metadata", "document"],
        )

        formatted_all: list[list[dict]] = []
        for hits in results:
            formatted = []
            for hit in hits:
                # pymilvus Entity.get() 在某些版本不支持默认值参数，
                # 使用 try/except 安全访问各字段。
                meta_raw = "{}"
                doc_raw = ""
                try:
                    meta_raw = hit.entity.get("metadata")
                except Exception as e:
                    logger.warning("[Milvus] 读取 metadata 字段失败: {}", str(e)[:150])
                try:
                    doc_raw = hit.entity.get("document")
                except Exception as e:
                    logger.warning("[Milvus] 读取 document 字段失败: {}", str(e)[:150])
                if isinstance(meta_raw, str):
                    meta_raw = json.loads(meta_raw)
                formatted.append({
                    "id": hit.id,
                    "similarity": round(hit.distance, 4),
                    "metadata": meta_raw or {},
                    "document": doc_raw or "",
                })
            formatted_all.append(formatted)
        return formatted_all

    def get_all_test_cases(self, product_id: str | None = None, limit: int = 100) -> list[dict]:
        """获取所有用例向量（product_id 为空时不过滤，避免空表达式报错）。"""
        self._ensure_connected()
        collection = self._get_collection("test_cases")
        # Milvus query 要求 expr 为 string 类型；无过滤条件时传空字符串
        expr = f'metadata["product_id"] == "{_safe_id(product_id, "product_id")}"' if product_id else ""
        results = collection.query(expr=expr, output_fields=["id", "metadata", "document"], limit=limit)
        parsed = []
        for r in results:
            meta = r.get("metadata", {})
            if isinstance(meta, str):
                meta = json.loads(meta)
            parsed.append({"id": r["id"], "metadata": meta or {}, "document": r.get("document", "")})
        return parsed

    def delete_test_case(self, case_id: str) -> None:
        """删除用例向量。

        v2.8.51 修复：必须 flush 才能让 delete 在 query/num_entities 中生效。
        """
        self._ensure_connected()
        collection = self._get_collection("test_cases")
        collection.delete(f'id == "{_safe_id(case_id, "case_id")}"')
        collection.flush()

    def delete_test_case_from_kb(self, case_id: str) -> None:
        """从知识库删除用例。"""
        self.delete_test_case(case_id)

    def delete_test_case_by_product(self, product_id: str) -> int:
        """按 product_id 批量删除用例向量。

        Args:
            product_id: 产品 ID

        Returns:
            实际删除的向量数
        """
        self._ensure_connected()
        collection = self._get_collection("test_cases")
        safe_pid = _safe_id(product_id, "product_id")
        # v3.4.1 修复 L-6：改为循环分批删除
        count = self._delete_by_expr_paged(
            collection,
            f'metadata["product_id"] == "{safe_pid}"',
            f"按 product_id 删除用例向量(product={product_id})",
        )
        logger.info("[Milvus] 按 product_id 批量删除用例向量: product={}, count={}", product_id, count)
        return count

    def delete_test_case_by_project(self, project_id: str) -> int:
        """按 project_id 批量删除用例向量（v2.45 新增，用于项目恢复活动时清理）。

        Args:
            project_id: 项目 ID

        Returns:
            实际删除的向量数
        """
        self._ensure_connected()
        collection = self._get_collection("test_cases")
        safe_pid = _safe_id(project_id, "project_id")
        # v3.4.1 修复 L-6：改为循环分批删除
        count = self._delete_by_expr_paged(
            collection,
            f'metadata["project_id"] == "{safe_pid}"',
            f"按 project_id 删除用例向量(project={project_id})",
        )
        logger.info("[Milvus] 按 project_id 批量删除用例向量: project={}, count={}", project_id, count)
        return count

    def delete_cases_by_req_id(self, req_id: str) -> None:
        """按 req_id 批量删除用例向量（优化 2：重新生成前清理旧用例）。

        Args:
            req_id: 需求 ID

        Note:
            Milvus delete 不返回删除计数，仅保证删除完成后无残留。
            调用方应在重新生成用例前调用，避免同一 req_id 产生重复用例。
        """
        self._ensure_connected()
        collection = self._get_collection("test_cases")
        collection.delete(f'metadata["req_id"] == "{_safe_id(req_id, "req_id")}"')
        collection.flush()

    # ========================================================================
    # 模式向量操作
    # ========================================================================

    def add_pattern(self, pattern_id: str, name: str, pattern_json: dict,
                    category: str = "crud") -> None:
        """添加测试模式。"""
        self._ensure_connected()
        text = json.dumps(pattern_json, ensure_ascii=False)
        meta = {"name": name, "category": category, "doc_type": "pattern"}
        self._upsert("patterns", [pattern_id], [text], [meta])

    def search_patterns(self, query: str, top_k: int = 3,
                        product_id: str | None = None) -> list[dict]:
        """检索测试模式。

        Args:
            query: 查询文本
            top_k: Top-K
            product_id: 按产品过滤（可选）。测试模式默认全局共享，
                        传入 product_id 时仅返回该产品关联的模式。
        """
        self._ensure_connected()
        where_filter = f'metadata["product_id"] == "{_safe_id(product_id, "product_id")}"' if product_id else None
        return self._search("patterns", query, top_k=top_k, where_filter=where_filter)

    # 兼容旧调用名（与 ChromaDB 接口对齐前的历史命名）
    def search_similar_patterns(self, query: str, top_k: int = 5) -> list[dict]:
        """检索相似模式（兼容旧接口，等价于 search_patterns）。"""
        return self.search_patterns(query, top_k=top_k)

    # ========================================================================
    # 需求模式向量操作（doc-02 §6 需求模式库）
    # ========================================================================

    def add_req_pattern(self, pattern_id: str, name: str, pattern_json: dict,
                        modules: list[str] | None = None) -> None:
        """添加需求模式到向量库。

        Args:
            pattern_id: 模式 ID
            name: 模式名称
            pattern_json: 模式 JSON 数据
            modules: 适用的模块列表
        """
        self._ensure_connected()
        text = f"{name}: {json.dumps(pattern_json, ensure_ascii=False)}"
        meta = {
            "name": name,
            "doc_type": "req_pattern",
            "modules": ",".join(modules) if modules else "",
        }
        self._upsert("req_pats", [pattern_id], [text], [meta])

    def search_req_patterns(self, query: str, top_k: int = 3,
                            module: str | None = None) -> list[dict]:
        """检索需求模式。

        Args:
            query: 查询文本
            top_k: Top-K
            module: 按模块过滤（可选）
        """
        self._ensure_connected()
        where_filter = None  # modules 以逗号分隔字符串存储，无法用 expr 直接过滤
        results = self._search("req_pats", query, top_k=top_k, where_filter=where_filter)
        if module:
            results = [
                r for r in results
                if module in (r.get("metadata", {}).get("modules", "") or "").split(",")
            ]
        return results

    def delete_req_pattern(self, pattern_id: str) -> None:
        """删除需求模式。"""
        self._ensure_connected()
        collection = self._get_collection("req_pats")
        collection.delete(f'id == "{_safe_id(pattern_id, "pattern_id")}"')
        # v2.8.51: 必须 flush 才能让 delete 在 query/num_entities 中生效
        # （与 delete_requirement / delete_test_case 保持一致，BOUNDED 一致性下否则仍返回已删除记录）
        collection.flush()

    # ========================================================================
    # 错误案例操作
    # ========================================================================

    def add_error_case(self, error_id: str, original_case_id: str,
                       error_type: str, error_detail: str,
                       corrected_json: dict | None = None,
                       metadata: dict | None = None,
                       product_id: str | None = None) -> None:
        """添加错误案例。

        Args:
            product_id: 产品 ID（显式传入以强制产品隔离；缺省时回退 metadata 中的 product_id）
        """
        self._ensure_connected()
        text = error_detail
        meta = metadata or {}
        meta.update({
            "error_id": error_id,
            "original_case_id": original_case_id,
            "error_type": error_type,
            "doc_type": "error_case",
        })
        # 产品隔离：显式 product_id 优先，否则取 metadata 中的；兜底 PROD-DEFAULT
        meta["product_id"] = product_id or meta.get("product_id", "") or "PROD-DEFAULT"
        self._upsert("errors", [error_id], [text], [meta])

    def search_similar_errors(self, query: str, top_k: int = 3,
                              product_id: str | None = None) -> list[dict]:
        """检索相似错误案例。

        Args:
            query: 查询文本
            top_k: Top-K
            product_id: 按产品过滤（可选）
        """
        self._ensure_connected()
        where_filter = f'metadata["product_id"] == "{_safe_id(product_id, "product_id")}"' if product_id else None
        return self._search("errors", query, top_k=top_k, where_filter=where_filter)

    def delete_error_case(self, error_id: str) -> None:
        """删除错误案例。"""
        self._ensure_connected()
        collection = self._get_collection("errors")
        collection.delete(f'id == "{_safe_id(error_id, "error_id")}"')
        # v2.8.51: 必须 flush 才能让 delete 在 query/num_entities 中生效
        collection.flush()

    # ========================================================================
    # 健康检查 / 工具方法
    # ========================================================================

    def is_empty(self) -> bool:
        """判断所有 Collection 是否为空。"""
        self._ensure_connected()
        for coll_key in COLLECTION_CONFIGS:
            coll_name = self._collection_name(coll_key)
            if utility.has_collection(coll_name):
                collection = Collection(coll_name)
                collection.load()
                if collection.num_entities > 0:
                    return False
        return True

    def is_model_ready(self) -> bool:
        """判断嵌入模型是否可用。"""
        try:
            self._ensure_model()
            return True
        except Exception:
            return False

    def count_requirements(self, product_id: str | None = None, status: str | None = None) -> int:
        """统计需求数量。

        Args:
            product_id: 按产品过滤（可选）
            status: 按状态过滤（可选，如 "archived"）；v2.8.50 新增
        """
        try:
            self._ensure_connected()
            collection = self._get_collection("requirements")
            conditions = []
            if product_id:
                conditions.append(f'metadata["product_id"] == "{_safe_id(product_id, "product_id")}"')
            if status:
                conditions.append(f'metadata["status"] == "{_safe_id(status, "status")}"')
            if conditions:
                expr = " and ".join(conditions)
                results = collection.query(expr=expr, output_fields=["id"], limit=16384)
                return len(results)
            return collection.num_entities
        except Exception:
            return 0

    def count_test_cases(self) -> int:
        """统计用例数量。"""
        try:
            self._ensure_connected()
            return self._get_collection("test_cases").num_entities
        except Exception:
            return 0

    def is_product_first_requirement(self, product_id: str) -> bool:
        """判断是否为该产品的首个需求（冷启动判断）。

        v2.8.50: 按 status=archived 过滤——KB 只检索已归档需求，
        若产品有需求向量但均为非 archived 状态（如 analyzing/draft），
        仍应判定为冷启动（KB 无法提供任何补全）。

        Args:
            product_id: 产品 ID
        """
        return self.count_requirements(product_id, status="archived") == 0

    def _srr_for_metadata(self, srr_json: dict) -> dict:
        """为 Milvus metadata 构建轻量化 SRR（metadata 总上限 65536 字节）。

        大文档（如架构设计文档）的 SRR JSON 可能达到 400KB+，
        直接存入 Milvus metadata 会触发 (code=1100) 报错。

        方案：只保留 KB 补全 / ClarifyAgent 实际用到的结构化字段，
        移除 _raw_text、dependencies 等仅用于人工阅读的大字段。
        （v2.40 起 additional_details 字段已移除，无需特别处理）

        实测：REQ-1784008720572-8967 从 309KB → 29KB（保留全部有用信息）。
        """
        # ClarifyAgent 实际消费的字段（见 clarify_agent.py _extract_baseline_block）
        # + document_full[:3000] 送给 LLM 的上下文
        KEEP_TOP_KEYS = {
            "srr_version",
            "requirement",
            "overview",
            "fp_summary",
            "fp_index",
            "flows",
            "boundary_conditions",
            "business_rules",
            "data_constraints",
            "preconditions",
            "acceptance_criteria",
            "test_points_hint",
            "notes",
        }
        return {k: v for k, v in srr_json.items() if k in KEEP_TOP_KEYS}

    # v3.2 N10 优化上升级：向量写入 / 问答回答的源文本需尽量完整，
    # 不能只截取需求概述/前N条规则，否则 LLM 出现"命中 source 但说未找到具体规则"。
    # 因此把功能点概要、完整业务规则、非功能约束、角色、UI/接口规则一并纳入。
    # 上限放宽到 20000 字符（仍远低于 Milvus document 列 65535 与 metadata 65536 上限），
    # 避免超大文档触发 (code=1100) 上限报错；核心细则（概述+功能点+业务规则）置于前部。
    _SRR_TEXT_MAX_CHARS = 20000

    def _srr_to_text(self, srr_json: dict) -> str:
        """SRR JSON → 嵌入/问答源文本（N10 修复：纳入功能点级与完整规则，减少截断丢弃）。

        产出内容优先级（置于前部，靠后被保护性截断影响最小）：
          需求标题/概述 → 各功能点概要(fp_summary 名称+类型+summary) → 流程(flows) →
          完整业务规则(不再限前10条) → 边界条件 → 数据约束 → 非功能约束 →
          角色(user_roles) → UI 规则 → API 定义。
        """
        parts = []
        req = srr_json.get("requirement", {})
        if isinstance(req, dict):
            if req.get("title"):
                parts.append(f"需求标题: {req['title']}")
            if req.get("summary"):
                parts.append(f"需求概述: {req['summary']}")

        # 功能点概要（v2.22+ srr_json 顶层含 fp_summary；兼容 fp_index）
        fps = srr_json.get("fp_summary") or srr_json.get("fp_index") or []
        if fps:
            fp_lines = []
            for f in fps:
                if not isinstance(f, dict):
                    continue
                name = f.get("name", "") or f.get("fp_name", "")
                ftype = f.get("fp_type", "")
                fsum = f.get("summary", "")
                fp_lines.append(
                    f"{name}(类型={ftype})" + (f": {fsum}" if fsum else "")
                )
            if fp_lines:
                parts.append("功能点: " + "；".join(fp_lines))

        flows = srr_json.get("flows", {})
        if isinstance(flows, dict):
            for flow_name, flow_data in flows.items():
                if isinstance(flow_data, dict):
                    steps = flow_data.get("steps", [])
                    if steps:
                        steps_text = "; ".join(
                            s.get("description", "") for s in steps if isinstance(s, dict)
                        )
                        if steps_text:
                            parts.append(f"[{flow_name}] {steps_text}")

        business_rules = srr_json.get("business_rules", [])
        if business_rules:
            parts.append("业务规则: " + "; ".join(str(r) for r in business_rules))

        boundary = srr_json.get("boundary_conditions", [])
        if boundary:
            parts.append("边界条件: " + "; ".join(str(b) for b in boundary))

        data_constraints = srr_json.get("data_constraints", [])
        if data_constraints:
            parts.append("数据约束: " + "; ".join(str(d) for d in data_constraints))

        non_func = srr_json.get("non_functional", {})
        if isinstance(non_func, dict):
            nf_pairs = [f"{k}:{v}" for k, v in non_func.items() if v]
            if nf_pairs:
                parts.append("非功能约束: " + "; ".join(nf_pairs))

        user_roles = srr_json.get("user_roles", [])
        if user_roles:
            parts.append("角色: " + "; ".join(str(r) for r in user_roles))

        ui_rules = srr_json.get("ui_rules", [])
        if ui_rules:
            parts.append("UI规则: " + "; ".join(str(u) for u in ui_rules))

        api_defs = srr_json.get("api_definitions", [])
        if api_defs:
            parts.append("接口: " + "; ".join(str(a) for a in api_defs))

        text = "\n".join(part for part in parts if part)
        # 保护性截断（超大文档），确保不超 Milvus document 列上限
        if len(text) > self._SRR_TEXT_MAX_CHARS:
            text = text[:self._SRR_TEXT_MAX_CHARS]
        return text


# 全局单例
milvus_store = MilvusVectorStore()
# 兼容别名：上游模块统一通过 `from app.db.milvus_store import vector_store` 引用。
# ChromaDB 已弃用（已由 Milvus 完全替代），这里 vector_store 指向同一 Milvus 单例。
vector_store = milvus_store

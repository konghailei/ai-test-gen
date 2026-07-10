"""ChromaDB 嵌入式向量库封装。

提供需求/用例/模式/错误案例的向量存储和相似度检索能力。
采用嵌入式模式（持久化到磁盘），零配置，适合 MVP 阶段。

嵌入模型支持两种模式（通过 EMBEDDING_MODEL_TYPE 配置）：
- chroma_default: ChromaDB 内置 ONNX all-MiniLM-L6-v2（英文优化，零配置）
- local: 本地 sentence-transformers 模型（推荐 BAAI/bge-large-zh-v1.5，中文优化）

设计依据：
- docs/05-technical-selection.md §4（向量数据库选型 ChromaDB → pgvector → Milvus）
- docs/05-technical-selection.md §5（嵌入模型推荐 bge-large-zh-v1.5）
- docs/02-knowledge-base.md（向量库 4 个 Collections 设计）
- templates/knowledge-base-schema.json:8-95（4 个 Collection）
"""

from __future__ import annotations
from typing import Any
import json
import os
from pathlib import Path
import chromadb
from chromadb.config import Settings as ChromaSettings
from app.config import settings
from app.utils.logger import logger


# ONNX 嵌入模型的缓存路径（chromadb 默认下载位置）
_ONNX_MODEL_DIR = Path.home() / ".cache" / "chroma" / "onnx_models" / "all-MiniLM-L6-v2" / "onnx"
_ONNX_MODEL_FILE = _ONNX_MODEL_DIR / "model.onnx"

# 本地嵌入模型缓存
_LOCAL_EMBEDDING_MODEL: Any = None


def _get_local_embedding_function() -> Any | None:
    """获取本地 sentence-transformers 嵌入模型。

    配置 EMBEDDING_MODEL_TYPE=local 时使用。
    推荐中文模型：BAAI/bge-large-zh-v1.5（1024 维）。
    需要安装：pip install sentence-transformers torch

    Returns:
        chromadb.EmbeddingFunction | None: 嵌入函数，加载失败返回 None
    """
    global _LOCAL_EMBEDDING_MODEL
    if _LOCAL_EMBEDDING_MODEL is not None:
        return _LOCAL_EMBEDDING_MODEL

    model_name = os.getenv("EMBEDDING_MODEL_NAME", "BAAI/bge-large-zh-v1.5")
    device = os.getenv("EMBEDDING_DEVICE", "cpu")

    try:
        from chromadb.utils import embedding_functions
        # 使用 sentence-transformers 作为 ChromaDB 的嵌入函数
        _LOCAL_EMBEDDING_MODEL = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=model_name,
            device=device,
        )
        logger.info(
            "本地嵌入模型已加载: model={}, device={}, dim={}",
            model_name,
            device,
            1024 if "bge-large" in model_name else "auto",
        )
        return _LOCAL_EMBEDDING_MODEL
    except ImportError as e:
        logger.warning(
            "本地嵌入模型加载失败（回退到 ChromaDB 默认模型）: {}. "
            "请安装: pip install sentence-transformers torch",
            str(e)[:80],
        )
        return None
    except Exception as e:
        logger.warning(
            "本地嵌入模型加载异常（回退到 ChromaDB 默认模型）: {}", str(e)[:120]
        )
        return None


class VectorStore:
    """ChromaDB 向量库封装。

    管理 4 个 Collection：
    - kb_requirements: 历史需求向量
    - kb_test_cases: 历史用例向量
    - kb_test_patterns: 测试模式向量
    - kb_error_cases: 错误案例向量

    嵌入模型（由 EMBEDDING_MODEL_TYPE 控制，见 .env.example）：
    - chroma_default: 默认 all-MiniLM-L6-v2（384 维，英文，零配置）
    - local: sentence-transformers（推荐 BAAI/bge-large-zh-v1.5，中文，1024 维）
    """

    def __init__(self) -> None:
        """初始化 ChromaDB 客户端和 Collections。"""
        self._client: chromadb.ClientAPI | None = None
        self._collections: dict[str, Any] = {}
        # 嵌入模型类型：'chroma_default' 或 'local'
        self._embedding_type: str = "chroma_default"
        # 本地嵌入函数（延迟加载）
        self._embedding_function: Any = None
        # ONNX 模型就绪标志：None=未检查, True=就绪, False=未下载
        self._onnx_model_ready: bool | None = None
        # 是否已尝试过加载本地模型
        self._local_model_tried: bool = False

    def _is_onnx_model_ready(self) -> bool:
        """检查 ChromaDB 默认 ONNX 嵌入模型是否已下载到本地缓存。

        chromadb 默认使用 ONNXMiniLM_L6_V2，首次使用时会从 S3 下载
        ~79MB 模型文件。若未下载，写/查向量库会阻塞数十分钟。

        Returns:
            bool: True 表示模型已就绪，可正常使用向量库
        """
        if self._onnx_model_ready is not None:
            return self._onnx_model_ready
        ready = _ONNX_MODEL_FILE.exists()
        self._onnx_model_ready = ready
        if not ready:
            logger.warning(
                "ONNX 嵌入模型未下载（{}），向量库操作将跳过。"
                "请运行 python -c \"import chromadb; chromadb.DefaultEmbeddingFunction()\" 触发下载。",
                _ONNX_MODEL_FILE,
            )
        return ready

    def _is_model_ready(self) -> bool:
        """检查嵌入模型是否就绪（支持两种模型类型）。

        根据 EMBEDDING_MODEL_TYPE 配置选择检查方式：
        - chroma_default: 检查 ONNX 模型缓存文件是否存在
        - local: 尝试加载 sentence-transformers 模型

        Returns:
            bool: True 表示模型已就绪
        """
        embedding_type = os.getenv("EMBEDDING_MODEL_TYPE", "chroma_default").lower()

        if embedding_type == "local":
            if not self._local_model_tried:
                self._embedding_function = _get_local_embedding_function()
                self._local_model_tried = True
            return self._embedding_function is not None

        # chroma_default
        return self._is_onnx_model_ready()

    def _get_embedding_function(self) -> Any | None:
        """获取配置的嵌入函数（用于 ChromaDB collection 创建）。

        Returns:
            Any | None: 嵌入函数，使用默认模型时返回 None（ChromaDB 默认 ONNX）
        """
        embedding_type = os.getenv("EMBEDDING_MODEL_TYPE", "chroma_default").lower()
        if embedding_type == "local":
            if self._is_model_ready():
                return self._embedding_function
            # local 模式但模型未就绪时，回退到 chromadb 默认
        # chroma_default: 返回 None 让 ChromaDB 使用其内置的 ONNX 模型
        return None

    def _ensure_client(self) -> chromadb.ClientAPI:
        """懒加载 ChromaDB 客户端。

        Returns:
            chromadb.ClientAPI: 持久化客户端实例
        """
        if self._client is None:
            logger.info("初始化 ChromaDB 客户端: {}", settings.CHROMA_PATH)

            # 获取嵌入函数（None 表示使用 ChromaDB 默认 ONNX 模型）
            emb_fn = self._get_embedding_function()
            model_hint = "local sentence-transformers" if emb_fn else "chromadb default ONNX"
            logger.info("嵌入模型: {}", model_hint)

            self._client = chromadb.PersistentClient(
                path=settings.CHROMA_PATH,
                settings=ChromaSettings(anonymized_telemetry=False),
            )

            # 初始化 4 个 Collection
            # 注意：embedding_function=None 会导致 upsert 报错
            # "You must provide an embedding function"
            # → 只有 emb_fn 非 None 时才传入，否则让 ChromaDB 使用默认 ONNX 模型
            base_meta = {"hnsw:space": "cosine"}
            coll_kwargs = {"metadata": base_meta}
            if emb_fn is not None:
                coll_kwargs["embedding_function"] = emb_fn

            self._collections = {
                "requirements": self._client.get_or_create_collection(
                    name="kb_requirements",
                    metadata={"description": "历史需求文档向量存储", **base_meta},
                    **({} if emb_fn is None else {"embedding_function": emb_fn}),
                ),
                "test_cases": self._client.get_or_create_collection(
                    name="kb_test_cases",
                    metadata={"description": "历史测试用例向量存储", **base_meta},
                    **({} if emb_fn is None else {"embedding_function": emb_fn}),
                ),
                "test_patterns": self._client.get_or_create_collection(
                    name="kb_test_patterns",
                    metadata={"description": "测试模式向量存储", **base_meta},
                    **({} if emb_fn is None else {"embedding_function": emb_fn}),
                ),
                "error_cases": self._client.get_or_create_collection(
                    name="kb_error_cases",
                    metadata={"description": "错误案例向量存储", **base_meta},
                    **({} if emb_fn is None else {"embedding_function": emb_fn}),
                ),
            }
            logger.info("ChromaDB Collections 初始化完成（4 个）")
        return self._client

    @property
    def requirements(self):
        """需求 Collection。"""
        self._ensure_client()
        return self._collections["requirements"]

    @property
    def test_cases(self):
        """用例 Collection。"""
        self._ensure_client()
        return self._collections["test_cases"]

    @property
    def test_patterns(self):
        """测试模式 Collection。"""
        self._ensure_client()
        return self._collections["test_patterns"]

    @property
    def error_cases_collection(self):
        """错误案例 Collection。"""
        self._ensure_client()
        return self._collections["error_cases"]

    # ========================================================================
    # 需求向量操作
    # ========================================================================

    def add_requirement(self, req_id: str, srr_json: dict, metadata: dict | None = None) -> None:
        """添加需求到向量库。

        Args:
            req_id: 需求 ID
            srr_json: SRR JSON 数据
            metadata: 元数据（product_id / modules / priority 等）
        """
        text = self._srr_to_text(srr_json)
        meta = metadata or {}
        meta["req_id"] = req_id
        meta["doc_type"] = "requirement"
        # 将完整 SRR 序列化存入 metadata，供 ClarifyAgent KB 自动补全读取
        # 注意：ChromaDB metadata 值仅支持 str / int / float / bool
        meta["srr_json"] = json.dumps(srr_json, ensure_ascii=False)
        self.requirements.upsert(
            ids=[req_id],
            documents=[text],
            metadatas=[meta],
        )

    def get_all_requirements(self, product_id: str | None = None, limit: int = 100) -> list[dict]:
        """获取知识库中的所有需求条目。

        Args:
            product_id: 按产品过滤（可选）
            limit: 最大返回条数

        Returns:
            list[dict]: 需求条目列表，每项含 id / metadata / document
        """
        self._ensure_client()
        try:
            where = {"where": {"product_id": product_id}} if product_id else {}
            results = self.requirements.get(**where, limit=limit)
            items = []
            for i, rid in enumerate(results.get("ids", [])):
                meta = (results.get("metadatas") or [{}])[i] or {}
                doc = (results.get("documents") or [""])[i] or ""
                items.append({
                    "id": rid,
                    "title": meta.get("req_id", rid),
                    "product_id": meta.get("product_id", ""),
                    "modules": meta.get("modules", ""),
                    "priority": meta.get("priority", ""),
                    "document": doc[:500],
                    "metadata": {k: v for k, v in meta.items() if k != "srr_json"},
                })
            return items
        except Exception as e:
            logger.warning("[VectorStore] 获取所有需求失败: {}", str(e)[:120])
            return []

    def delete_requirement_from_kb(self, req_id: str) -> None:
        """从知识库中删除需求条目。"""
        self._ensure_client()
        try:
            self.requirements.delete(ids=[req_id])
            logger.info("[VectorStore] 已从知识库删除需求: req_id={}", req_id)
        except Exception as e:
            logger.warning("[VectorStore] 删除需求失败: {}", str(e)[:120])

    def get_all_test_cases(self, product_id: str | None = None, limit: int = 100) -> list[dict]:
        """获取知识库中的所有用例条目。

        Args:
            product_id: 按产品过滤（可选）
            limit: 最大返回条数

        Returns:
            list[dict]: 用例条目列表
        """
        self._ensure_client()
        try:
            where = {"where": {"product_id": product_id}} if product_id else {}
            results = self.test_cases.get(**where, limit=limit)
            items = []
            for i, cid in enumerate(results.get("ids", [])):
                meta = (results.get("metadatas") or [{}])[i] or {}
                doc = (results.get("documents") or [""])[i] or ""
                items.append({
                    "id": cid,
                    "title": meta.get("case_id", cid),
                    "product_id": meta.get("product_id", ""),
                    "document": doc[:500],
                    "metadata": {k: v for k, v in meta.items()},
                })
            return items
        except Exception as e:
            logger.warning("[VectorStore] 获取所有用例失败: {}", str(e)[:120])
            return []

    def delete_test_case_from_kb(self, case_id: str) -> None:
        """从知识库中删除用例条目。"""
        self._ensure_client()
        try:
            self.test_cases.delete(ids=[case_id])
            logger.info("[VectorStore] 已从知识库删除用例: case_id={}", case_id)
        except Exception as e:
            logger.warning("[VectorStore] 删除用例失败: {}", str(e)[:120])

    def search_similar_requirements(
        self,
        query: str,
        product_id: str | None = None,
        modules: list[str] | None = None,
        top_k: int = 5,
        status: str | None = None,
    ) -> list[dict]:
        """检索相似需求。

        Args:
            query: 查询文本（通常是需求 summary）
            product_id: 按产品过滤（可选）
            modules: 按模块过滤（可选）
            top_k: 返回前 K 条
            status: 按状态过滤（可选，如 "archived" "draft"）

        Returns:
            list[dict]: 相似需求列表，每项含 id / similarity / metadata / document
        """
        if not self._is_model_ready():
            return []
        # 构建 where 过滤条件（ChromaDB 只支持单 key 或 $and 数组）
        conditions = []
        if product_id:
            conditions.append({"product_id": product_id})
        if status:
            conditions.append({"status": status})

        if len(conditions) == 1:
            where_filter = conditions[0]
        elif len(conditions) > 1:
            where_filter = {"$and": conditions}
        else:
            where_filter = None

        # modules 无法用 ChromaDB where 直接过滤（metadata 不支持 list 类型，
        # 且 modules 以逗号分隔字符串存储），放大召回量后在内存中按模块过滤。
        fetch_k = top_k * 5 if modules else top_k
        results = self.requirements.query(
            query_texts=[query],
            n_results=fetch_k,
            where=where_filter,
        )
        formatted = self._format_results(results)

        if modules:
            wanted = {m for m in modules}
            filtered = []
            for r in formatted:
                raw = r.get("metadata", {}).get("modules", "")
                # modules 以逗号分隔字符串存储（见 req_agent.py add_requirement 调用）
                stored = raw.split(",") if isinstance(raw, str) and raw else (raw or [])
                stored_set = {m.strip() for m in stored if isinstance(m, str) and m.strip()}
                if wanted & stored_set:
                    filtered.append(r)
            formatted = filtered[:top_k]
        return formatted

    def delete_requirement(self, req_id: str) -> None:
        """从向量库删除需求。

        Args:
            req_id: 需求 ID
        """
        if not self._is_model_ready():
            return
        self.requirements.delete(ids=[req_id])

    # ========================================================================
    # 用例向量操作
    # ========================================================================

    def add_test_case(self, case_id: str, case_json: dict, metadata: dict | None = None) -> None:
        """添加用例到向量库。

        Args:
            case_id: 用例 ID
            case_json: UTCF 用例 JSON
            metadata: 元数据（req_id / type / priority 等）
        """
        if not self._is_model_ready():
            return
        text = self._case_to_text(case_json)
        meta = metadata or {}
        meta["case_id"] = case_id
        meta["doc_type"] = "test_case"
        self.test_cases.upsert(
            ids=[case_id],
            documents=[text],
            metadatas=[meta],
        )

    def search_similar_cases(self, query: str, top_k: int = 5,
                             case_type: str | None = None,
                             req_id: str | None = None,
                             product_id: str | None = None) -> list[dict]:
        """检索相似用例。

        Args:
            query: 查询文本
            top_k: 返回前 K 条
            case_type: 按用例类型过滤（可选）
            req_id: 按需求 ID 过滤（可选）
            product_id: 按产品过滤（可选，v2.2 新增——产品隔离必须）

        Returns:
            list[dict]: 相似用例列表
        """
        if not self._is_model_ready():
            return []
        where_filter = {}
        if case_type:
            where_filter["case_type"] = case_type
        if req_id:
            where_filter["req_id"] = req_id
        if product_id:
            where_filter["product_id"] = product_id

        results = self.test_cases.query(
            query_texts=[query],
            n_results=top_k,
            where=where_filter if where_filter else None,
        )
        return self._format_results(results)

    def delete_test_case(self, case_id: str) -> None:
        """从向量库删除用例。

        Args:
            case_id: 用例 ID
        """
        if not self._is_model_ready():
            return
        self.test_cases.delete(ids=[case_id])

    # ========================================================================
    # 测试模式操作
    # ========================================================================

    def add_pattern(self, pattern_id: str, name: str, pattern_json: dict,
                    category: str = "crud") -> None:
        """添加测试模式。

        Args:
            pattern_id: 模式 ID
            name: 模式名称
            pattern_json: 模式 JSON 数据
            category: 模式类别
        """
        if not self._is_model_ready():
            return
        self.test_patterns.upsert(
            ids=[pattern_id],
            documents=[f"{name}: {pattern_json.get('applicable_scenario', '')} "
                       f"{' '.join(pattern_json.get('test_dimensions', []))}"],
            metadatas=[{
                "pattern_id": pattern_id,
                "name": name,
                "doc_type": "test_pattern",
                "category": category,
            }],
        )

    def search_patterns(self, query: str, top_k: int = 3,
                        product_id: str | None = None) -> list[dict]:
        """检索测试模式。

        Args:
            query: 查询文本
            top_k: 返回前 K 条
            product_id: 按产品过滤（可选）。测试模式默认全局共享，
                        传入 product_id 时仅返回该产品关联的模式。

        Returns:
            list[dict]: 匹配的测试模式列表
        """
        if not self._is_model_ready():
            return []
        where_filter = None
        if product_id:
            where_filter = {"product_id": product_id}
        results = self.test_patterns.query(
            query_texts=[query],
            n_results=top_k,
            where=where_filter,
        )
        return self._format_results(results)

    # ========================================================================
    # 错误案例操作（文档 §2.2 error_cases Collection）
    # ========================================================================

    def add_error_case(self, error_id: str, original_case_id: str,
                       error_type: str, error_detail: str,
                       corrected_json: dict | None = None,
                       metadata: dict | None = None) -> None:
        """添加错误案例到向量库。

        错误案例用于 ReviewAgent 避免重复错误（docs/02 §2.2）。

        Args:
            error_id: 错误案例 ID
            original_case_id: 原始用例 ID
            error_type: 错误类型 (missing_precondition/wrong_boundary/redundant/ambiguous/incomplete/conflicting)
            error_detail: 错误详情
            corrected_json: 修正后的 UTCF JSON（可选）
            metadata: 扩展元数据（可选）
        """
        if not self._is_model_ready():
            return
        text = (
            f"原用例: {original_case_id}\n"
            f"错误类型: {error_type}\n"
            f"错误详情: {error_detail}\n"
            f"修正: {self._case_to_text(corrected_json) if corrected_json else ''}"
        )
        meta = metadata or {}
        meta["error_id"] = error_id
        meta["original_case_id"] = original_case_id
        meta["error_type"] = error_type
        meta["doc_type"] = "error_case"
        self.error_cases_collection.upsert(
            ids=[error_id],
            documents=[text],
            metadatas=[meta],
        )

    def search_similar_errors(self, query: str, top_k: int = 3,
                              product_id: str | None = None) -> list[dict]:
        """检索相似错误案例。

        Args:
            query: 查询文本
            top_k: 返回前 K 条
            product_id: 按产品过滤（可选，v2.2 新增）

        Returns:
            list[dict]: 匹配的错误案例列表
        """
        if not self._is_model_ready():
            return []
        where_filter = None
        if product_id:
            where_filter = {"product_id": product_id}
        results = self.error_cases_collection.query(
            query_texts=[query],
            n_results=top_k,
            where=where_filter,
        )
        return self._format_results(results)

    def delete_error_case(self, error_id: str) -> None:
        """从向量库删除错误案例。

        Args:
            error_id: 错误案例 ID
        """
        if not self._is_model_ready():
            return
        self.error_cases_collection.delete(ids=[error_id])

    # ========================================================================
    # 状态查询（用于冷启动判断）
    # ========================================================================

    def is_empty(self) -> bool:
        """判断向量库是否为空（冷启动模式判断）。

        综合检查 requirements / test_cases / test_patterns 三个 Collection，
        任一 Collection 有数据即视为非空。

        Returns:
            bool: True 表示 KB 为空（冷启动模式）
        """
        self._ensure_client()
        try:
            return (
                self.requirements.count() == 0
                and self.test_cases.count() == 0
                and self.test_patterns.count() == 0
            )
        except Exception:
            # 向量库未就绪时视为非空（避免冷启动循环）
            return False

    def count_requirements(self, product_id: str | None = None) -> int:
        """统计需求数量。

        Args:
            product_id: 按产品过滤（可选）

        Returns:
            int: 需求数量
        """
        self._ensure_client()
        if product_id:
            results = self.requirements.get(where={"product_id": product_id})
            return len(results.get("ids", []))
        try:
            return self.requirements.count()
        except Exception:
            return 0

    def count_test_cases(self) -> int:
        """统计用例数量。"""
        self._ensure_client()
        try:
            return self.test_cases.count()
        except Exception:
            return 0

    def is_product_first_requirement(self, product_id: str) -> bool:
        """判断是否为该产品的首个需求（冷启动判断）。

        Args:
            product_id: 产品 ID

        Returns:
            bool: True 表示该产品下无历史需求
        """
        return self.count_requirements(product_id) == 0

    # ========================================================================
    # 工具方法
    # ========================================================================

    def _srr_to_text(self, srr_json: dict) -> str:
        """将 SRR 转为向量化的文本。

        按 templates/knowledge-base-schema.json:29 的 text_template 组织。

        Args:
            srr_json: SRR JSON

        Returns:
            str: 用于向量化的文本
        """
        req = srr_json.get("requirement", {})
        flows = srr_json.get("flows", {})
        happy = flows.get("happy_path", {})
        steps = happy.get("steps", [])
        steps_text = "; ".join(f"{s.get('action','')}->{s.get('expect','')}" for s in steps)

        return (
            f"{req.get('title','')}\n"
            f"{req.get('summary','')}\n"
            f"前置条件: {', '.join(srr_json.get('preconditions', []))}\n"
            f"正常流程: {steps_text}\n"
            f"异常流程: {', '.join(f.get('name','') for f in flows.get('alternate_flows', []))}\n"
            f"业务规则: {', '.join(r.get('rule','') for r in srr_json.get('business_rules', []))}\n"
            f"测试关注点: {', '.join(t.get('point','') for t in srr_json.get('test_points_hint', []))}"
        )

    def _case_to_text(self, case_json: dict) -> str:
        """将用例转为向量化的文本。

        Args:
            case_json: UTCF 用例 JSON

        Returns:
            str: 用于向量化的文本
        """
        if not case_json:
            return ""
        steps = case_json.get("steps", [])
        steps_text = "; ".join(
            f"{s.get('action','')}->{s.get('expected','')}" for s in steps if isinstance(s, dict)
        )
        return (
            f"{case_json.get('title','')}\n"
            f"类型: {case_json.get('type','')}\n"
            f"前置条件: {', '.join(case_json.get('preconditions', []))}\n"
            f"步骤: {steps_text}\n"
            f"断言: {', '.join(a.get('expected_value','') for a in case_json.get('assertions', []))}"
        )

    def _format_results(self, results: dict) -> list[dict]:
        """格式化 ChromaDB 查询结果。

        Args:
            results: ChromaDB 原始查询结果

        Returns:
            list[dict]: 标准化结果列表，每项含 id / similarity / metadata / document
        """
        if not results or not results.get("ids") or not results["ids"][0]:
            return []

        formatted = []
        ids = results["ids"][0]
        distances = results.get("distances", [[]])[0] if results.get("distances") else []
        metadatas = results.get("metadatas", [[]])[0] if results.get("metadatas") else []
        documents = results.get("documents", [[]])[0] if results.get("documents") else []

        for i, rid in enumerate(ids):
            # ChromaDB 距离越小越相似，转换为相似度（0-1）
            distance = distances[i] if i < len(distances) else 1.0
            similarity = max(0.0, 1.0 - distance)
            formatted.append({
                "id": rid,
                "similarity": round(similarity, 4),
                "metadata": metadatas[i] if i < len(metadatas) else {},
                "document": documents[i] if i < len(documents) else "",
            })
        return formatted


# 全局单例 — 根据 VECTOR_STORE 配置选择后端
# 用法：from app.db.vector_store import vector_store
import os

_store_backend = os.getenv("VECTOR_STORE", "chromadb").lower()

if _store_backend == "milvus":
    from app.db.milvus_store import milvus_store
    vector_store = milvus_store  # type: ignore[assignment]
else:
    vector_store = VectorStore()

"""Milvus 向量存储适配器。

实现与 ChromaDB VectorStore 相同的接口，通过 pymilvus 连接 Milvus Standalone。
配合 bge-large-zh-v1.5 嵌入模型，1024 维中文语义向量。

设计依据：
- docs/05-technical-selection.md §4（向量数据库选型）
- docs/05-technical-selection.md §5.1（bge-large-zh-v1.5 推荐）
- docs/02-knowledge-base.md §2（双引擎存储架构）

Collection 设计：
- aitestgen_requirements  — 需求向量（ID: requirement ID）
- aitestgen_test_cases    — 用例向量（ID: test case ID）
- aitestgen_patterns      — 测试模式向量（ID: pattern ID）
- aitestgen_errors        — 错误案例向量（ID: error case ID）
"""

from __future__ import annotations

import json
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
    "errors": {
        "description": "错误案例向量",
        "dim": 1024,
    },
}

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
    """Milvus 向量存储，兼容原 ChromaDB VectorStore 接口。"""

    def __init__(self):
        self._model: SentenceTransformer | None = None
        self._connected = False
        self._connect_lock = threading.Lock()
        self._model_lock = threading.Lock()

    @property
    def test_patterns(self):
        """兼容 ChromaDB API 的属性访问，用于预热向量库。"""
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

    def _ensure_model(self) -> SentenceTransformer:
        """确保嵌入模型已加载（线程安全，避免并发重复加载）。

        优先加载本地预置模型，避免运行时联网下载。
        """
        if self._model is None:
            with self._model_lock:
                if self._model is None:
                    import os
                    model_name = settings.EMBEDDING_MODEL_NAME
                    device = settings.EMBEDDING_DEVICE

                    # 1) EMBEDDING_MODEL_NAME 直接指向本地目录（基础镜像内置或宿主机挂载）
                    #    例：/models/bge-large-zh-v1.5
                    if os.path.isabs(model_name) and os.path.isfile(os.path.join(model_name, "config.json")):
                        logger.info("[Milvus] 加载本地模型路径: {}", model_name)
                        self._model = SentenceTransformer(model_name, device=device)
                        return self._model

                    # 2) 兼容旧版缓存目录结构（早期 modelscope 预下载遗留）
                    local_paths = [
                        "/root/.cache/huggingface/models/BAAI--bge-large-zh-v1.5/snapshots/master",
                        "/root/.cache/huggingface/hub/models--BAAI--bge-large-zh-v1.5/snapshots/main",
                    ]
                    local_model = None
                    for p in local_paths:
                        if os.path.isfile(os.path.join(p, "config.json")):
                            local_model = p
                            break

                    if local_model:
                        logger.info("[Milvus] 加载预置模型: {}", local_model)
                        self._model = SentenceTransformer(local_model, device=device)
                    else:
                        logger.info("[Milvus] 下载模型: {} (device={})", model_name, device)
                        self._model = SentenceTransformer(model_name, device=device)
        return self._model

    def _collection_name(self, name: str) -> str:
        """生成带前缀的 Collection 名称。"""
        prefix = settings.MILVUS_COLLECTION_PREFIX
        return f"{prefix}_{name}"

    def _ensure_collections(self) -> None:
        """确保所有 Collection 已创建。"""
        for coll_key, cfg in COLLECTION_CONFIGS.items():
            coll_name = self._collection_name(coll_key)
            if not utility.has_collection(coll_name):
                self._create_collection(coll_name, cfg["dim"])
                logger.info("[Milvus] 创建 Collection: {}", coll_name)

    def _create_collection(self, coll_name: str, dim: int) -> None:
        """创建 Collection（id + vector + metadata + document）。"""
        fields = [
            FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=256),
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=dim),
            FieldSchema(name="metadata", dtype=DataType.JSON),
            FieldSchema(name="document", dtype=DataType.VARCHAR, max_length=65535),
        ]
        schema = CollectionSchema(fields, description=coll_name)

        # 创建 IVF_FLAT 索引（适合 < 10 万向量）
        index_params = {
            "metric_type": "COSINE",
            "index_type": "IVF_FLAT",
            "params": {"nlist": 128},
        }
        collection = Collection(coll_name, schema)
        collection.create_index("vector", index_params)
        collection.load()

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """批量文本 → 向量。"""
        model = self._ensure_model()
        embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return embeddings.tolist()

    def _get_collection(self, coll_key: str) -> Collection:
        """获取已加载的 Collection。"""
        self._ensure_connected()
        coll_name = self._collection_name(coll_key)
        collection = Collection(coll_name)
        collection.load()
        return collection

    def _upsert(self, coll_key: str, ids: list[str], docs: list[str], metadatas: list[dict]) -> None:
        """写入/更新向量。"""
        if not ids:
            return
        collection = self._get_collection(coll_key)
        vectors = self._embed(docs)

        # Upsert：先删后插
        collection.delete(f'id in [{",".join(repr(i) for i in ids)}]')
        collection.insert([
            ids,
            vectors,
            [json.dumps(m, ensure_ascii=False) for m in metadatas],
            docs,
        ])
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

        search_params = {"metric_type": "COSINE", "params": {"nprobe": 16}}

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
                meta_raw = hit.entity.get("metadata", "{}")
                meta = json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
                formatted.append({
                    "id": hit.id,
                    "similarity": round(hit.distance, 4),
                    "metadata": meta or {},
                    "document": hit.entity.get("document", ""),
                })
        return formatted

    # ========================================================================
    # 需求向量操作（兼容原 VectorStore 接口）
    # ========================================================================

    def add_requirement(self, req_id: str, srr_json: dict, metadata: dict | None = None) -> None:
        """添加需求到向量库。"""
        self._ensure_connected()
        text = self._srr_to_text(srr_json)
        meta = metadata or {}
        meta["req_id"] = req_id
        meta["doc_type"] = "requirement"
        # srr_json 可能超过 Milvus VARCHAR(65535) 限制，存入 document 字段而非 metadata
        self._upsert("requirements", [req_id], [text], [meta])

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
            query: 查询文本
            product_id: 按产品过滤
            modules: 按模块过滤
            top_k: Top-K
            status: 按状态过滤（archived/draft）

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
        where_filter = " && ".join(conditions) if conditions else None

        # modules 无法用 Milvus expr 直接过滤（以逗号分隔字符串存储于 metadata），
        # 放大召回量后在内存中按模块过滤，与 ChromaDB 版本保持一致。
        fetch_k = top_k * 5 if modules else top_k
        formatted = self._search("requirements", query, top_k=fetch_k, where_filter=where_filter)

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

    def get_all_requirements(self, product_id: str | None = None, limit: int = 100) -> list[dict]:
        """获取所有需求向量（product_id 为空时不过滤，避免空表达式报错）。"""
        self._ensure_connected()
        collection = self._get_collection("requirements")
        expr = f'metadata["product_id"] == "{_safe_id(product_id, "product_id")}"' if product_id else None
        results = collection.query(expr=expr, output_fields=["id", "metadata", "document"], limit=limit)
        parsed = []
        for r in results:
            meta = r.get("metadata", {})
            if isinstance(meta, str):
                meta = json.loads(meta)
            parsed.append({"id": r["id"], "metadata": meta or {}, "document": r.get("document", "")})
        return parsed

    def delete_requirement(self, req_id: str) -> None:
        """删除需求向量。"""
        self._ensure_connected()
        collection = self._get_collection("requirements")
        collection.delete(f'id == "{_safe_id(req_id, "req_id")}"')

    def delete_requirement_from_kb(self, req_id: str) -> None:
        """从知识库删除需求（与 delete_requirement 同）。"""
        self.delete_requirement(req_id)

    # ========================================================================
    # 用例向量操作
    # ========================================================================

    def add_test_case(self, case_id: str, case_json: dict, metadata: dict | None = None) -> None:
        """添加用例到向量库。"""
        self._ensure_connected()
        text = json.dumps(case_json, ensure_ascii=False)
        meta = metadata or {}
        meta["case_id"] = case_id
        meta["doc_type"] = "test_case"
        self._upsert("test_cases", [case_id], [text], [meta])

    def search_similar_cases(self, query: str, top_k: int = 5,
                             case_type: str | None = None,
                             req_id: str | None = None,
                             product_id: str | None = None) -> list[dict]:
        """检索相似用例。

        Args:
            query: 查询文本
            top_k: Top-K
            case_type: 按用例类型过滤（可选）
            req_id: 按需求 ID 过滤（可选）
            product_id: 按产品过滤（可选，产品隔离必须）
        """
        self._ensure_connected()
        conditions = []
        if case_type:
            conditions.append(f'metadata["case_type"] == "{_safe_id(case_type, "case_type")}"')
        if req_id:
            conditions.append(f'metadata["req_id"] == "{_safe_id(req_id, "req_id")}"')
        if product_id:
            conditions.append(f'metadata["product_id"] == "{_safe_id(product_id, "product_id")}"')
        where_filter = " && ".join(conditions) if conditions else None
        return self._search("test_cases", query, top_k=top_k, where_filter=where_filter)

    def get_all_test_cases(self, product_id: str | None = None, limit: int = 100) -> list[dict]:
        """获取所有用例向量（product_id 为空时不过滤，避免空表达式报错）。"""
        self._ensure_connected()
        collection = self._get_collection("test_cases")
        expr = f'metadata["product_id"] == "{_safe_id(product_id, "product_id")}"' if product_id else None
        results = collection.query(expr=expr, output_fields=["id", "metadata", "document"], limit=limit)
        parsed = []
        for r in results:
            meta = r.get("metadata", {})
            if isinstance(meta, str):
                meta = json.loads(meta)
            parsed.append({"id": r["id"], "metadata": meta or {}, "document": r.get("document", "")})
        return parsed

    def delete_test_case(self, case_id: str) -> None:
        """删除用例向量。"""
        self._ensure_connected()
        collection = self._get_collection("test_cases")
        collection.delete(f'id == "{_safe_id(case_id, "case_id")}"')

    def delete_test_case_from_kb(self, case_id: str) -> None:
        """从知识库删除用例。"""
        self.delete_test_case(case_id)

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
    # 错误案例操作
    # ========================================================================

    def add_error_case(self, error_id: str, original_case_id: str,
                       error_type: str, error_detail: str,
                       corrected_json: dict | None = None,
                       metadata: dict | None = None) -> None:
        """添加错误案例。"""
        self._ensure_connected()
        text = error_detail
        meta = metadata or {}
        meta.update({
            "error_id": error_id,
            "original_case_id": original_case_id,
            "error_type": error_type,
            "doc_type": "error_case",
        })
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

    def count_requirements(self, product_id: str | None = None) -> int:
        """统计需求数量。

        Args:
            product_id: 按产品过滤（可选）
        """
        try:
            self._ensure_connected()
            collection = self._get_collection("requirements")
            if product_id:
                expr = f'metadata["product_id"] == "{_safe_id(product_id, "product_id")}"'
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

        Args:
            product_id: 产品 ID
        """
        return self.count_requirements(product_id) == 0

    def _srr_to_text(self, srr_json: dict) -> str:
        """SRR JSON → 嵌入文本（与 ChromaDB 版本保持一致）。"""
        parts = []
        req = srr_json.get("requirement", {})
        if req.get("title"):
            parts.append(f"需求标题: {req['title']}")
        if req.get("summary"):
            parts.append(f"需求概述: {req['summary']}")
        flows = srr_json.get("flows", {})
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
            parts.append(f"业务规则: {'; '.join(str(r) for r in business_rules[:10])}")
        boundary = srr_json.get("boundary_conditions", [])
        if boundary:
            parts.append(f"边界条件: {'; '.join(str(b) for b in boundary[:10])}")
        additional = srr_json.get("additional_details", "")
        if additional:
            parts.append(f"补充细节: {additional[:500]}")
        return "\n".join(parts)


# 全局单例
milvus_store = MilvusVectorStore()

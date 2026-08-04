"""混合检索模块 — Milvus 向量检索 + PostgreSQL BM25 关键词检索 + RRF 融合。

设计依据：
- docs/02-knowledge-base.md §3.1（混合检索架构）
- docs/02-knowledge-base.md §9.3（HNSW 索引参数）
- docs/02-knowledge-base.md §9.5（Re-rank 优化）

降级策略（docs/02 §2.6 简化版）：
- 向量检索失败 → 仅用 BM25
- BM25 检索失败 → 仅用向量
- 两者都失败 → 返回空（上层走人工澄清兜底）
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.milvus_store import vector_store
from app.utils.logger import logger


class HybridSearcher:
    """混合检索器，融合向量语义检索和 BM25 关键词检索。"""

    RRF_K = 60  # RRF 融合参数
    # BM25 文本搜索配置：优先 chinese(zhparser)，回退 simple
    # 在首次 BM25 查询时动态检测并缓存
    _ts_config: str | None = None

    async def search_requirements(
        self,
        query: str,
        product_id: str | None = None,
        modules: list[str] | None = None,
        top_k: int = 20,
        status: str | None = None,
        session: AsyncSession | None = None,
        doc_type: str | None = None,
    ) -> list[dict]:
        """混合检索需求：向量 + BM25 → RRF 融合 → Cross-Encoder 重排 → Top-K。

        Args:
            query: 查询文本
            product_id: 按产品过滤
            modules: 按模块过滤
            top_k: 返回数量
            status: 按状态过滤
            session: PG 异步会话（有值时启用 BM25 混合检索）
            doc_type: 按文档类型过滤（v2.53 新增）
                - "requirement": 仅检索总 SRR 记录（避免 FP 向量混入）
                - "functional_point": 仅检索功能点 SRR 记录
                - None: 同时检索两类（默认，向后兼容）

        Returns:
            list[dict]: 融合后的检索结果
        """
        # 0. 缓存查询（modules + doc_type 参与缓存键，避免跨模块/跨类型缓存污染）
        from app.db.kb_cache import kb_cache
        modules_key = ",".join(sorted(modules)) if modules else ""
        doc_type_key = doc_type or ""
        cached = await kb_cache.get(query, product_id or "", "requirements", modules_key, doc_type_key)
        if cached is not None:
            logger.debug("[HybridSearch] 缓存命中: query={!r}", query[:60])
            return cached[:top_k]

        # 1. Milvus 向量检索（异步 to_thread）
        vector_results: list[dict] = []
        try:
            vector_results = await asyncio.to_thread(
                vector_store.search_similar_requirements,
                query=query, product_id=product_id, modules=modules,
                top_k=top_k * 2, status=status, doc_type=doc_type,
            )
        except Exception as e:
            logger.warning("[HybridSearch] 向量检索失败，仅用 BM25: {}", str(e)[:200])

        # 2. PG BM25 关键词检索
        bm25_results: list[dict] = []
        if session:
            try:
                bm25_results = await self._bm25_search(
                    session, query, product_id, top_k * 2, status, modules
                )
            except Exception as e:
                logger.warning("[HybridSearch] BM25 检索失败，仅用向量: {}", str(e)[:200])

        # 3. 两者都失败 → 返回空（上层走人工澄清兜底）
        if not vector_results and not bm25_results:
            logger.info("[HybridSearch] 向量 + BM25 均无结果: query={!r}", query[:60])
            return []

        # 4. RRF 融合
        fused = self._rrf_fuse(vector_results, bm25_results)

        # 5. Cross-Encoder 重排
        if settings.RERANKER_ENABLED and len(fused) > settings.MATCH_FINAL_N:
            try:
                from app.db.reranker import reranker
                fused = await asyncio.to_thread(
                    reranker.rerank, query, fused, top_n=settings.MATCH_FINAL_N
                )
            except Exception as e:
                logger.warning("[HybridSearch] 重排失败，使用 RRF 排序: {}", str(e)[:200])

        # 6. 写入缓存
        await kb_cache.set(query, product_id or "", "requirements", fused, modules_key, doc_type_key)

        return fused[:top_k]

    async def _bm25_search(
        self,
        session: AsyncSession,
        query: str,
        product_id: str | None,
        top_k: int,
        status: str | None,
        modules: list[str] | None = None,
    ) -> list[dict]:
        """PostgreSQL BM25 全文检索（优先 zhparser 中文分词，回退 simple）。

        Args:
            session: PG 会话
            query: 查询文本
            product_id: 按产品过滤
            top_k: 返回数量
            status: 按状态过滤
            modules: 按模块过滤（只匹配 modules 数组有交集的记录）

        Returns:
            list[dict]: BM25 检索结果，格式与向量检索一致
        """
        ts_config = await self._get_ts_config(session)
        # modules 过滤：传入逗号分隔字符串，SQL 中用 string_to_array + && (数组交集) 过滤
        module_filter = ",".join(modules) if modules else None
        # 使用 CAST 显式指定参数类型，避免 asyncpg AmbiguousParameterError
        sql = text(f"""
            SELECT id,
                   ts_rank(search_vec, plainto_tsquery('{ts_config}', :q)) AS rank,
                   title,
                   srr_json,
                   status,
                   product_id
            FROM requirements
            WHERE search_vec @@ plainto_tsquery('{ts_config}', :q)
              AND (CAST(:product_id AS VARCHAR) IS NULL OR product_id = :product_id)
              AND (CAST(:status AS VARCHAR) IS NULL OR status = :status)
              AND (CAST(:module_filter AS VARCHAR) IS NULL OR EXISTS (SELECT 1 FROM json_array_elements_text(modules) AS m WHERE m = ANY(string_to_array(:module_filter, ','))))
            ORDER BY rank DESC
            LIMIT :top_k
        """)
        # 使用 savepoint 隔离 BM25 失败，避免污染外层事务
        async with session.begin_nested():
            result = await session.execute(sql, {
                "q": query,
                "product_id": product_id,
                "status": status,
                "module_filter": module_filter,
                "top_k": top_k,
            })
            rows = result.all()
        return [
            {
                "id": r.id,
                "similarity": float(r.rank),
                "document": r.title or "",
                "metadata": {
                    "req_id": r.id,
                    "status": r.status,
                    "product_id": r.product_id,
                    "srr_json": json.dumps(r.srr_json, ensure_ascii=False) if r.srr_json else "",
                },
            }
            for r in rows
        ]

    async def _get_ts_config(self, session: AsyncSession) -> str:
        """检测可用的中文文本搜索配置，缓存结果。

        优先使用 chinese(zhparser)，不可用时回退 simple。
        """
        if self._ts_config is not None:
            return self._ts_config
        try:
            result = await session.execute(
                text("SELECT 1 FROM pg_ts_config WHERE cfgname = 'chinese'")
            )
            if result.fetchone():
                self._ts_config = "chinese"
            else:
                self._ts_config = "simple"
                logger.info("[HybridSearch] zhparser 未安装，BM25 使用 simple 配置")
        except Exception:
            self._ts_config = "simple"
        return self._ts_config

    def _rrf_fuse(
        self, vector_results: list[dict], bm25_results: list[dict]
    ) -> list[dict]:
        """Reciprocal Rank Fusion (k=60) 融合两路检索结果。

        RRF 公式: score(d) = Σ 1/(k + rank_i(d))

        Args:
            vector_results: 向量检索结果
            bm25_results: BM25 检索结果

        Returns:
            list[dict]: 融合后的结果，按 RRF 分数降序排列
        """
        scores: dict[str, float] = {}
        items: dict[str, dict] = {}

        for rank, item in enumerate(vector_results):
            item_id = item["id"]
            scores[item_id] = scores.get(item_id, 0) + 1.0 / (self.RRF_K + rank + 1)
            items[item_id] = item

        for rank, item in enumerate(bm25_results):
            item_id = item["id"]
            scores[item_id] = scores.get(item_id, 0) + 1.0 / (self.RRF_K + rank + 1)
            if item_id not in items:
                items[item_id] = item

        fused = [
            {**items[item_id], "rrf_score": score}
            for item_id, score in sorted(scores.items(), key=lambda x: -x[1])
        ]
        return fused


hybrid_searcher = HybridSearcher()

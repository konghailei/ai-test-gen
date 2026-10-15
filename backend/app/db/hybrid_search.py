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
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.milvus_store import vector_store
from app.utils.logger import logger


class HybridSearcher:
    """混合检索器，融合向量语义检索和 BM25 关键词检索。"""

    RRF_K = 60  # RRF 融合参数
    # 时间衰减参数（v2.8.55：修复应用层粗暴覆盖排序破坏 RRF 相关性）
    # 半衰期 T=180 天：180 天前的文档权重衰减到 0.5，360 天前衰减到 0.25
    # 衰减作为 RRF 分数的乘数，而非覆盖排序，保证相关性仍是主导信号
    TIME_DECAY_HALF_LIFE_DAYS = 180
    _ts_config: str | None = None

    async def search_requirements(
        self,
        query: str,
        product_id: str | None = None,
        modules: list[str] | None = None,
        top_k: int = 20,
        status: str | None = None,
        session: AsyncSession | None = None,
        doc_type: str | list[str] | None = None,
    ) -> list[dict]:
        """混合检索需求：向量 + BM25 → RRF 融合 → Cross-Encoder 重排 → Top-K。

        Args:
            query: 查询文本
            product_id: 按产品过滤
            modules: 按模块过滤
            top_k: 返回数量
            status: 按状态过滤
            session: PG 异步会话（有值时启用 BM25 混合检索）
            doc_type: 按文档类型过滤（v2.8.53 新增；v4.1 支持多值）
                - "requirement": 仅检索总 SRR 记录（避免 FP 向量混入）
                - "functional_point": 仅检索功能点 SRR 记录
                - list[str]: 多类型并集（需求权威层 L0+L1）
                - None: 同时检索两类（默认，向后兼容）

        Returns:
            list[dict]: 融合后的检索结果
        """
        # 0. 缓存查询（modules + doc_type + status + top_k 参与缓存键，避免跨筛选/跨长度缓存污染）
        from app.db.kb_cache import kb_cache
        modules_key = ",".join(sorted(modules)) if modules else ""
        # v4.1：doc_type 可能是 list，缓存键需稳定字符串化（排序后 join）
        if isinstance(doc_type, (list, tuple, set)):
            doc_type_key = ",".join(sorted(t for t in doc_type if t))
        else:
            doc_type_key = doc_type or ""
        cached = await kb_cache.get(
            query, product_id or "", "requirements", modules_key, doc_type_key,
            status=status or "", top_k=top_k,
        )
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
        bm25_level = ""  # v3.4.1 修复 L-11：BM25 命中级别（Level C 兜底不进长 TTL 缓存）
        if session:
            try:
                bm25_results, bm25_level = await self._bm25_search(
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

        # v2.8.55: 在 RRF 融合后应用时间衰减因子
        # 不再用应用层覆盖排序，而是用乘数方式让时间作为辅助信号
        # 相关性主导（RRF 分数）× 时间新鲜度（衰减因子）
        fused = self._apply_time_decay(fused)

        # 5. Cross-Encoder 重排（基于时间衰减后的排序结果）
        # v3.4.1 修复 M-1：top_n 取 max(top_k, MATCH_FINAL_N)。原实现写死 MATCH_FINAL_N=5，
        # 与请求 top_k 无关地把融合结果截到 5 条，而截断后的列表又按原 top_k 分桶写入
        # 24h 缓存，使 top_k=20/40 的检索最多只返回 5 条。
        rerank_n = max(top_k, settings.MATCH_FINAL_N)
        if settings.RERANKER_ENABLED and len(fused) > settings.MATCH_FINAL_N:
            try:
                from app.db.reranker import reranker
                fused = await asyncio.to_thread(
                    reranker.rerank, query, fused, top_n=rerank_n
                )
            except Exception as e:
                logger.warning("[HybridSearch] 重排失败，使用 RRF 排序: {}", str(e)[:200])

        # 6. 写入缓存（只有两路检索至少一路有效，避免降级结果污染长 TTL 缓存；同时写入 status/top_k）
        # v3.4.1 修复 L-11：Level C 是关键词 OR 兜底，命中的结果过于宽松，
        # 写 24h 长缓存会把低质量结果反复喂给上层，故 Level C 命中时跳过缓存写。
        cacheable = (bool(vector_results) or bool(bm25_results)) and bm25_level != "or_tokens"
        if cacheable:
            await kb_cache.set(
                query, product_id or "", "requirements", fused,
                modules_key, doc_type_key,
                status=status or "", top_k=top_k,
            )

        return fused[:top_k]

    async def _bm25_search(
        self,
        session: AsyncSession,
        query: str,
        product_id: str | None,
        top_k: int,
        status: str | None,
        modules: list[str] | None = None,
    ) -> tuple[list[dict], str]:
        """PostgreSQL BM25 全文检索（优先 zhparser 中文分词，回退 simple）。

        安全修复（C1）：不再用 f-string 拼接 ts_config 到 SQL 文本中，改用
        `plainto_tsquery(CAST(:ts_config AS regconfig), ...)` 参数绑定。
        PostgreSQL 的 CAST(... AS regconfig) 会对配置名做严格标识符校验，
        非法值直接报错，无法闭合字符串或注入后续 SQL 语句。
        CAST(... AS BIGINT) 限制 top_k 为整数范围，避免绕过 LIMIT 类型。

        Args:
            session: PG 会话
            query: 查询文本
            product_id: 按产品过滤
            top_k: 返回数量（强转 BIGINT 正数）
            status: 按状态过滤
            modules: 按模块过滤（只匹配 modules 数组有交集的记录）

        Returns:
            tuple[list[dict], str]: (BM25 检索结果, 命中的查询级别)
                级别取值 "websearch" / "plainto" / "or_tokens"，均未命中时为 ""
        """
        ts_config = await self._get_ts_config(session)
        # modules 过滤：传入逗号分隔字符串，SQL 中用 string_to_array + && (数组交集) 过滤
        module_filter = ",".join(modules) if modules else None
        # 强校验 top_k 为正整数（防止 SQL 注入/异常值绕过 LIMIT 语义）
        safe_top_k = max(1, int(top_k))
        # BM25 三级降级查询策略（避免长问句因 plainto_tsquery 的 AND 条件导致 0 命中）：
        #   Level A : websearch_to_tsquery  → 支持短语/引号/OR/排除，语义友好（PG11+ 可用）
        #   Level B : plainto_tsquery       → AND 连接所有词，精确（兼容旧PG）
        #   Level C : 提取核心词做 OR 查询  → 任一词命中即可，极度宽松兜底
        query_levels = []
        # Level A: websearch（若 PG 支持则首选；失败自动 savepoint 回退不抛错）
        query_levels.append(("websearch", "websearch_to_tsquery(CAST(:ts_config AS regconfig), :q)"))
        # Level B: plainto（AND 连接）
        query_levels.append(("plainto", "plainto_tsquery(CAST(:ts_config AS regconfig), :q)"))
        # Level C: OR 核心词兜底——用 regexp_split_to_table 对短句去停用词做 OR（由 Python 端替换 <OR_QUERY> 后执行）
        #          这里用 to_tsquery 的 | 连接所有单 token 的 OR 查询；若 query 过短则原样 plainto
        module_sql = "(CAST(:module_filter AS VARCHAR) IS NULL OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(COALESCE(modules::jsonb, '[]'::jsonb)) AS m WHERE m = ANY(string_to_array(:module_filter, ','))))"
        base_sql = f"""
            SELECT id,
                   ts_rank(search_vec, {{tsquery_fn}}) AS rank,
                   title,
                   srr_json,
                   status,
                   product_id
            FROM requirements
            WHERE search_vec @@ {{tsquery_fn}}
              AND (CAST(:product_id AS VARCHAR) IS NULL OR product_id = :product_id)
              AND (CAST(:status AS VARCHAR) IS NULL OR status = :status)
              AND {module_sql}
            ORDER BY rank DESC
            LIMIT CAST(:top_k AS BIGINT)
        """

        rows = []
        # v3.4.1 修复 L-11：记录命中的查询级别，Level C 兜底结果不写长 TTL 缓存
        matched_level = ""
        # 使用 savepoint 隔离每一级查询，任何一级失败都不影响外层
        for lvl_name, tsquery_fn in query_levels:
            async with session.begin_nested():
                try:
                    sql = text(base_sql.format(tsquery_fn=tsquery_fn))
                    result = await session.execute(sql, {
                        "q": query,
                        "ts_config": ts_config,
                        "product_id": product_id,
                        "status": status,
                        "module_filter": module_filter,
                        "top_k": safe_top_k,
                    })
                    rows = result.all()
                    if rows:
                        matched_level = lvl_name
                        break
                except Exception as e:  # 例如 websearch_to_tsquery 在不支持版本/特殊 query 语法时失败
                    logger.debug("[HybridSearch] BM25 {} query skipped: {}", lvl_name, e)
                    continue

        # Level C 兜底：若前两级仍 0 条，且 query 非纯符号则做关键词 OR 查询
        if not rows:
            # 从 query 中提取长度≥2 的中文/英文片段（去除纯标点/疑问词/停用词），用 | 拼接
            import re
            tokens = []
            for t in re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,}", query):
                if t not in {"什么","怎么","如何","为什么","是否","可以","能够","应该","支持","具体","哪些"}:
                    tokens.append(t)
            if tokens:
                # 保留最多 6 个关键词，避免 OR 太散
                tokens = tokens[:6]
                or_q = " | ".join(tokens)
                async with session.begin_nested():
                    try:
                        sql = text(base_sql.format(tsquery_fn=f"to_tsquery(CAST(:ts_config AS regconfig), :q2)"))
                        result = await session.execute(sql, {
                            "q2": or_q,
                            "ts_config": ts_config,
                            "product_id": product_id,
                            "status": status,
                            "module_filter": module_filter,
                            "top_k": safe_top_k,
                        })
                        rows = result.all()
                        if rows:
                            matched_level = "or_tokens"
                    except Exception as e:
                        logger.debug("[HybridSearch] BM25 Level-C OR query skipped: {}", e)
        results_formatted = [
            {
                "id": r.id,
                "similarity": float(r.rank),
                "document": r.title or "",
                "metadata": {
                    "req_id": r.id,
                    # v4.1（缺陷 A 修复）：BM25 路查的是 requirements 表（总 SRR），
                    # 类型恒为 requirement。原实现不写 type → 融合后被 _enrich_sources
                    # 当作「未知类型 → 非 requirement 分支」→ found=False 但又不是
                    # requirement/fp/crsr 三类型的孤儿检查范围 → 侥幸留下但标题显示
                    # 「(已删除)」，且权威层级取 AUTHORITY_BY_TYPE 默认 L1（错误）。
                    "type": "requirement",
                    "doc_type": "requirement",
                    "status": r.status,
                    "product_id": r.product_id,
                    "srr_json": json.dumps(r.srr_json, ensure_ascii=False) if r.srr_json else "",
                },
            }
            for r in rows
        ]
        return results_formatted, matched_level

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

        # v4.0.1：标记分数来源。KB 问答的相关度门槛（COSINE 余弦）只能作用于
        # 向量路结果；BM25 路的 `similarity` 是 PG `ts_rank`，量纲不可比且天然很小，
        # 若一并过滤会在「Milvus 不可用 → 仅 BM25 降级」时把可用结果全部滤空。
        for rank, item in enumerate(vector_results):
            item_id = item["id"]
            item["_score_kind"] = "vector"
            scores[item_id] = scores.get(item_id, 0) + 1.0 / (self.RRF_K + rank + 1)
            items[item_id] = item

        for rank, item in enumerate(bm25_results):
            item_id = item["id"]
            scores[item_id] = scores.get(item_id, 0) + 1.0 / (self.RRF_K + rank + 1)
            if item_id not in items:
                item["_score_kind"] = "bm25"
                items[item_id] = item

        fused = [
            {**items[item_id], "rrf_score": score}
            for item_id, score in sorted(scores.items(), key=lambda x: -x[1])
        ]
        return fused

    def _apply_time_decay(self, fused: list[dict]) -> list[dict]:
        """对 RRF 融合结果应用时间衰减因子（v2.8.55）。

        设计目标：在功能点迭代多次的场景下，让较新版本的文档在相同相关性下优先。
        实现：用指数衰减作为 RRF 分数的乘数，而非应用层覆盖排序。
              - 相关性主导：RRF 分数仍是主要排序信号
              - 时间辅助：相同 RRF 分数下，上线时间新的优先
              - 半衰期 T=180 天：文档年龄每增加 180 天，权重衰减一半

        衰减公式：decay = 0.5 ** (age_days / T)
        最终分数：final_score = rrf_score * (0.5 + 0.5 * decay)
                  （0.5 基线保证旧文档不会完全被压制）

        Args:
            fused: RRF 融合后的结果列表

        Returns:
            list[dict]: 应用时间衰减后的结果列表，按新分数降序排列
        """
        if not fused:
            return fused

        now = datetime.now(timezone.utc)
        half_life = self.TIME_DECAY_HALF_LIFE_DAYS

        for item in fused:
            rrf_score = item.get("rrf_score", 0)
            meta = item.get("metadata", {}) or {}
            go_live = meta.get("go_live_date", "")

            # 解析上线时间（支持 ISO 格式字符串和 datetime 对象）
            age_days = 0
            if go_live:
                try:
                    if isinstance(go_live, str):
                        # 处理 ISO 格式，兼容带/不带时区的情况
                        go_live_dt = datetime.fromisoformat(
                            go_live.replace("Z", "+00:00")
                        )
                    elif isinstance(go_live, datetime):
                        go_live_dt = go_live
                    else:
                        go_live_dt = None

                    if go_live_dt:
                        # 统一时区
                        if go_live_dt.tzinfo is None:
                            go_live_dt = go_live_dt.replace(tzinfo=timezone.utc)
                        age_timedelta = now - go_live_dt
                        age_days = max(0, age_timedelta.days)
                except (ValueError, TypeError):
                    age_days = 0

            # 指数衰减：0.5 ** (age_days / T)
            decay = 0.5 ** (age_days / half_life) if half_life > 0 else 1.0
            # 0.5 基线 + 0.5 * decay，保证旧文档权重最低为 0.5
            time_factor = 0.5 + 0.5 * decay
            final_score = rrf_score * time_factor

            item["rrf_score"] = round(final_score, 6)
            # 保留时间衰减信息供调试和日志使用
            item["_time_decay"] = round(time_factor, 4)
            item["_age_days"] = age_days

        # 按应用时间衰减后的分数重新排序
        fused.sort(key=lambda x: -x.get("rrf_score", 0))
        return fused


hybrid_searcher = HybridSearcher()

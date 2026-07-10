"""ClarifyAgent 需求澄清 Agent。

在最终 SRR 生成之前，对需求（原始文本 + 初步结构化草稿）进行三级检测
（完整性/歧义性/遗漏），识别会阻碍生成完整、可测试 SRR 的缺口与模糊点，
并生成针对 KB 无法解决之缺口的人工问题列表。遵循 ClarifyGPT（ACL 2024）理念：
仅对模糊需求提问。

⚠️ 架构约定：本 Agent 运行于「最终 SRR 生成」之前，输入是 ReqAgent 产出的
初步结构化草稿（草稿 SRR）+ 原始需求文本。目标是找出「为生成完整 SRR 还缺
什么」，而非「审查已生成的 SRR」。对应流程：上传 → 解析草稿 → 本 Agent 检测
缺口 → KB 查询 → 能答则生成最终 SRR，查不到则人工澄清 → 最终 SRR。

设计依据：
- docs/01-requirement-parsing.md §4（ClarifyAgent 需求澄清闭环）
- docs/01-requirement-parsing.md §4.3（三级检测机制）
- docs/01-requirement-parsing.md §4.4（KB 自动补全）

核心原则（docs/01 §4.1）：
    1. 仅对阻碍 SRR 完整生成的点提问 — 不浪费用户时间
    2. KB 优先 — 能自动补全的不问人工
    3. 问题精准 — 基于缺口分析，不问无关问题
    4. 多轮迭代 — 最多 3 轮，质量达标才放行

输入：草稿 SRR JSON（含 requirement / flows 等初步字段）
输出：{status, questions, auto_augmentations, quality_report, kb_status}
      ⚠️ 输出格式与 QualityGate（§5.2）输入对齐（嵌套结构）
"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

from app.config import settings
from app.core.llm_client import llm_client
from app.db.vector_store import vector_store
from app.prompts import clarify_agent as prompts
from app.core import prompt_manager
from app.utils.logger import logger


class ClarifyAgent:
    """需求澄清 Agent。

    对应 docs/00-architecture-overview.md §4 的 ClarifyAgent 节点。

    工作流程（docs/01 §4.2）：
        ① 完整性检测 → ② 歧义性检测 → ③ 遗漏检测 →
        ④ KB 自动补全 → ⑤ 问题生成 → ⑥ 质量评分 → ⑦ 返回结果

    Usage:
        agent = ClarifyAgent()
        result = await agent.clarify(srr)
        if result["status"] == "needs_clarification":
            # 展示 result["questions"] 给用户回答
            ...
    """

    # 质量评分通过阈值（与 QualityGate.DEFAULT_PASS_SCORE 一致）
    PASS_THRESHOLD: float = settings.DEFAULT_PASS_SCORE

    def __init__(self) -> None:
        """初始化 ClarifyAgent。"""
        self._llm = llm_client

    async def clarify(self, srr: dict, session = None) -> dict:
        """对 SRR 执行需求澄清。

        Args:
            srr: SRR JSON 字典
            session: SQLite 异步会话（v2.6: 用于关系型数据增强）

        Returns:
            dict: 澄清结果，格式如下（与 QualityGate §5.2 输入对齐）：
                {
                    "status": "needs_clarification" | "passed",
                    "questions": [...],
                    "auto_augmentations": [...],
                    "quality_report": {
                        "overall_score": float,
                        "pass_threshold": float
                    },
                    "kb_status": {
                        "kb_empty": bool,
                        "product_first_requirement": bool
                    }
                }

        Note:
            - KB 优先：能从知识库推断的不问人工
            - 冷启动模式（KB 为空或产品首个需求）阈值放宽
            - Critical/Important 问题不限条数（v2.1：原 max_questions=5 限制已移除）
            - Optional 问题建议上限 20 个（可配置）
            - 返回值新增 kb_access_log 字段（v2.1）
        """
        req_id = srr.get("requirement", {}).get("id", "")
        title = srr.get("requirement", {}).get("title", "")
        logger.info("[ClarifyAgent] ▶ 开始澄清: req_id={}, title={}", req_id, title)

        # 初始化 KB 访问日志（v2.1 新增）
        kb_access_log: list[dict] = []

        # ① 判断 KB 状态（冷启动模式判定）
        product_id = srr.get("requirement", {}).get("product_id", "PROD-DEFAULT")
        import time
        _t0 = time.time()
        kb_empty = await asyncio.to_thread(self._safe_is_empty)
        _d1 = int((time.time() - _t0) * 1000)
        product_first = await asyncio.to_thread(self._safe_is_product_first, product_id)
        _d2 = int((time.time() - _t0) * 1000) - _d1
        cold_start = kb_empty or product_first
        logger.info("[ClarifyAgent] KB 状态: kb_empty={}, product_first={}, cold_start={}, product_id={}",
                    kb_empty, product_first, cold_start, product_id)
        kb_access_log.append({
            "action": "check_kb_status",
            "type": "check_kb_status",
            "label": "KB 状态检查",
            "query": "知识库中是否存在同类需求可以辅助分析当前需求？是否需要启用冷启动模式？",
            "query_detail": f"向量库{'为空' if kb_empty else '正常'}，当前产品{'首个需求（冷启动）' if product_first else '已有历史数据'}",
            "result_count": 0 if (kb_empty or product_first) else 1,
            "product_first_requirement": product_first,
            "duration_ms": _d1 + _d2,
            "status": "success",
        })

        # ② 规则检测：完整性 + 歧义性
        completeness = self._check_completeness(srr)
        ambiguities = self._detect_ambiguity(srr)
        logger.info("[ClarifyAgent] 规则检测: completeness_score={:.3f}, critical_missing={}, "
                    "important_missing={}, ambiguities={}",
                    completeness["completeness_score"],
                    len(completeness["critical_missing"]),
                    len(completeness["important_missing"]),
                    len(ambiguities))
        if ambiguities:
            logger.debug("[ClarifyAgent] 歧义详情: types={}",
                         [a["type"] for a in ambiguities])

        # ③ KB 检索：以每个缺失字段和歧义点为问题，向知识库提问
        import time, copy
        augmented_srr = copy.deepcopy(srr)

        # ③a 用需求摘要做全局搜索，供 SQLite 增强和问题查询复用
        summary = srr.get("requirement", {}).get("summary", "")
        global_similar = await asyncio.to_thread(self._search_similar_requirements, srr)

        # ③b SQLite 关系增强（先跑，供后续 Q&A 查询使用）
        _t_sql = time.time()
        sqlite_context = await self._enrich_with_sqlite(session, srr, global_similar) if session else {}
        _d_sql = int((time.time() - _t_sql) * 1000)

        _t3 = time.time()
        # ③ KB 查询生成（v2.10）：LLM 动态生成上下文感知的搜索查询。
        #    不再使用固定模板，LLM 失败时直接抛异常上报，确保查询质量。
        #    注入原始需求全文，让 KB 查询生成器能参考原始文档，避免对已有内容的重复提问。
        if session and not srr.get("_raw_text"):
            from sqlalchemy import select as _select
            from app.db.models import Requirement as _ReqModel
            req_id = srr.get("requirement", {}).get("id", "")
            if req_id:
                r = await session.execute(
                    _select(_ReqModel.raw_text).where(_ReqModel.id == req_id)
                )
                raw = r.scalar_one_or_none()
                if raw:
                    srr["_raw_text"] = raw
        kb_questions = await self._generate_kb_queries(
            srr, completeness, srr.get("_is_incremental", False)
        )

        _d3 = int((time.time() - _t3) * 1000)
        logger.info("[ClarifyAgent] 生成 KB 问题: count={}", len(kb_questions))

        # ④ 对每个问题查询 KB + SQLite，获取答案
        _t4 = time.time()
        kb_answers: list[dict] = await self._query_kb_for_answers(
            session, srr, kb_questions, global_similar, sqlite_context,
        )
        _d4 = int((time.time() - _t4) * 1000)
        logger.info("[ClarifyAgent] KB 回答: answered={}, unanswered={}, duration={}ms",
                    sum(1 for a in kb_answers if a.get("answered")),
                    sum(1 for a in kb_answers if not a.get("answered")),
                    _d4)

        # ⑤ 应用 KB 答案 → 自动补全 SRR
        _t5 = time.time()
        augmented_srr, augmentations = self._apply_kb_answers(augmented_srr, kb_answers)
        _d5 = int((time.time() - _t5) * 1000)
        logger.info("[ClarifyAgent] KB 应用: augmentations={}", len(augmentations))

        # ⑥ 将 Q&A 记录为 KB 查询日志（供前端展示）
        for qa in kb_answers:
            kb_access_log.append({
                "action": "kb_query",
                "type": "kb_query" if qa.get("answered") else "kb_unresolved",
                "label": "KB 问答" if qa.get("answered") else "KB 未解决",
                "query": qa.get("question", ""),
                "answer": qa.get("answer", ""),
                "sources": qa.get("sources", []),
                "answered": qa.get("answered", False),
                "auto_filled": qa.get("auto_filled", False),
                "result_count": len(qa.get("sources", [])),
                "duration_ms": _d4,
            })

        # ⑦ 补全后重新检测
        if augmentations:
            completeness = self._check_completeness(augmented_srr)
            ambiguities = self._detect_ambiguity(augmented_srr)
            logger.info("[ClarifyAgent] KB补全后重新检测: critical_missing={}, important_missing={}",
                        len(completeness["critical_missing"]),
                        len(completeness["important_missing"]))

        # ⑧ KB 未答上的问题 → 直接作为人工澄清问题（不需要 LLM 再生成一套）
        # KB 查询已经通过了 _generate_kb_queries 的质量审查和 _verify_kb_answers 的答案审核，
        # 能答的已自动补全，不能答的就是真正需要人工介入的缺口。
        unresolved_qa = [qa for qa in kb_answers if not qa.get("answered")]
        if unresolved_qa:
            questions = [
                {
                    "id": f"kb_{qa.get('field','gap').replace('.','_')}",
                    "field": qa.get("field", ""),
                    "priority": qa.get("priority", "important"),
                    "category": "completeness",
                    "question": qa.get("question", ""),
                    "context": qa.get("missing_desc", "知识库中未找到相关信息，请补充具体内容"),
                    "default_answer": qa.get("missing_desc", ""),
                    "input_type": "text",
                    "options": [],
                }
                for qa in unresolved_qa
            ]
            questions = self._ensure_critical_questions(
                questions, unresolved_qa, completeness,
            )
        else:
            questions = []
        logger.info("[ClarifyAgent] KB已回答: {}, 待人工澄清(LLM精炼): {}",
                    sum(1 for a in kb_answers if a.get("answered")), len(questions))

        # 计算质量评分
        quality_score = self._calc_quality_score(completeness, ambiguities, {"questions": questions, "quality_report": {}})

        # 判定状态：仍有需人工澄清的问题 → needs_clarification；否则 passed
        threshold = self.PASS_THRESHOLD
        if not questions and quality_score >= threshold:
            status = "passed"
        else:
            status = "needs_clarification"

        kb_answered_count = sum(1 for a in kb_answers if a.get("answered"))
        result = {
            "status": status,
            "questions": questions,
            "auto_augmentations": augmentations,
            # ⭐ v2.6：返回 KB 增强后的 SRR，确保补全字段持久化
            "augmented_srr": augmented_srr,
            # ⚠️ 嵌套结构，与 QualityGate._resolve_pass_score 输入对齐（修复问题1）
            "quality_report": {
                "overall_score": round(quality_score, 3),
                "pass_threshold": threshold,
            },
            # ⚠️ 嵌套结构，与 QualityGate._resolve_pass_score 输入对齐（修复问题1）
            "kb_status": {
                "kb_empty": kb_empty,
                "product_first_requirement": product_first,
            },
            # ⭐ v2.8：暴露真实 KB 计数，供 API 正确展示（避免用 questions.answered 误算）
            "kb_answered_count": kb_answered_count,
            "kb_total_count": len(kb_answers),
            # ⭐ v2.1 新增：KB 访问日志
            "kb_access_log": kb_access_log[:settings.KB_ACCESS_LOG_MAX_ENTRIES],
        }

        logger.info("[ClarifyAgent] ◀ 澄清完成 ▶ 下一节点 QualityGate: status={}, "
                    "overall_score={:.3f}, pass_threshold={}, questions={}, cold_start={}",
                    status, quality_score, threshold, len(questions), cold_start)
        return result

    # ========================================================================
    # 完整性检测（docs/01 §4.3.1）
    # ========================================================================

    def _check_completeness(self, srr: dict) -> dict:
        """检查 SRR 完整性 + 内容质量，返回缺失字段列表和评分。

        v2.8: 增加了 shallow_check（内容质量检测），字段存在但内容空洞也标记为缺失。

        Args:
            srr: SRR JSON 字典

        Returns:
            dict: {
                "critical_missing": [...],
                "important_missing": [...],
                "optional_missing": [...],
                "completeness_score": float,
                "passed": bool
            }
        """
        result = {
            "critical_missing": [],
            "important_missing": [],
            "optional_missing": [],
            "completeness_score": 1.0,
            "passed": True,
        }

        for level, fields in prompts.COMPLETENESS_CHECKLIST.items():
            for field, desc in fields.items():
                val = self._get_nested_value(srr, field)
                if not self._has_meaningful_content(field, val):
                    result[f"{level}_missing"].append({"field": field, "description": desc})

        # 评分
        missing_weight = (
            len(result["critical_missing"]) * 3
            + len(result["important_missing"]) * 1
            + len(result["optional_missing"]) * 0.3
        )
        total = sum(len(v) for v in prompts.COMPLETENESS_CHECKLIST.values())
        result["completeness_score"] = max(0.0, 1.0 - missing_weight / total)
        result["passed"] = len(result["critical_missing"]) == 0
        return result

    @staticmethod
    def _has_meaningful_content(field: str, val) -> bool:
        """检查字段是否有实质性内容（纯存在性守卫）。

        方向A（v2.9）：规则层只做存在性守卫，不再做内容质量/长度阈值判定。
        旧版曾对 list<2条、str<5字符等做阈值判定，导致大量机械误报
        （如 acceptance_criteria 只有 1 条详细内容也被报缺）。字段内容是否
        充分、是否存在语义遗漏，完全交给 LLM 在 _generate_questions 中判断
        ——LLM 能看到完整 SRR 全文，检测更准确。
        """
        if val is None:
            return False
        if isinstance(val, (str, list, dict)):
            return len(val) > 0
        return True

    # ========================================================================
    # 歧义性检测（docs/01 §4.3.2，规则部分）
    # ========================================================================

    def _detect_ambiguity(self, srr: dict) -> list[dict]:
        """歧义检测已迁移至 LLM（_generate_questions）。

        方向A（v2.9）：移除规则关键词匹配（AMBIGUITY_PATTERNS），避免
        「合理」「适当」「等」等词的机械误报与漏报。LLM 在生成澄清问题时
        基于语义判断「该表述是否导致测试预期不确定」，更准确、能理解上下文。
        本方法保留签名（供调用点兼容），固定返回空列表。
        """
        return []

    # ========================================================================
    # KB 检索
    # ========================================================================

    def _safe_is_empty(self) -> bool:
        """安全判断向量库是否为空（模型未就绪时返回 True）。"""
        try:
            return vector_store.is_empty()
        except Exception:
            return True

    def _safe_is_product_first(self, product_id: str) -> bool:
        """安全判断是否为产品首个需求（模型未就绪时返回 True）。"""
        try:
            return vector_store.is_product_first_requirement(product_id)
        except Exception:
            return True

    def _search_similar_requirements(self, srr: dict) -> list[dict]:
        """检索同类需求（供遗漏检测和 KB 补全）。"""
        summary = srr.get("requirement", {}).get("summary", "")
        product_id = srr.get("requirement", {}).get("product_id", "")
        modules = srr.get("requirement", {}).get("modules", [])
        try:
            return vector_store.search_similar_requirements(
                query=summary, product_id=product_id, modules=modules, top_k=5
            )
        except Exception as e:
            logger.debug("[ClarifyAgent] KB 检索相似需求失败（不阻断）: {}", str(e)[:120])
            return []

    def _search_patterns(self, srr: dict) -> list[dict]:
        """检索匹配的测试模式。"""
        summary = srr.get("requirement", {}).get("summary", "")
        try:
            return vector_store.search_patterns(query=summary, top_k=3)
        except Exception as e:
            logger.debug("[ClarifyAgent] KB 检索测试模式失败（不阻断）: {}", str(e)[:120])
            return []

    # ========================================================================
    # KB 查询生成 — LLM 动态生成上下文感知的搜索查询（v2.9）
    # ========================================================================
    # 替代固定模板 _formulate_kb_questions / _formulate_baseline_kb_questions。
    # 固定模板（如「XX的正常操作流程是怎样的？」）不带具体场景上下文，
    # 导致 ChromaDB 语义检索命中率偏低，尤其对于增量文档、MFA/SSO 等细分场景。
    # LLM 生成的查询包含功能名、关键特征、技术术语，检索更精准。
    # ========================================================================

    def _build_kb_query_user_prompt(
        self, srr: dict, completeness: dict, is_incremental: bool
    ) -> str:
        """构建 KB 查询生成的用户指令。

        将需求上下文 + 缺口信息组织成 prompt，让 LLM 生成具体、上下文感知的
        查询文本（直接用作 ChromaDB 语义检索 query），而非固定模板。
        """
        req = srr.get("requirement", {}) or {}
        title = req.get("title", "当前需求")
        summary = req.get("summary", "")

        # 提取关键上下文特征（帮助 LLM 在查询中融入具体细节）
        context_snippets: list[str] = []
        flows = srr.get("flows", {}) or {}
        hp = flows.get("happy_path", {}) or {}
        hp_steps = hp.get("steps", []) or []
        if hp_steps:
            step_descs = [
                s.get("description", "") for s in hp_steps[:3]
                if isinstance(s, dict) and s.get("description")
            ]
            if step_descs:
                context_snippets.append("正常流程: " + "; ".join(step_descs))

        af = flows.get("alternate_flows", []) or []
        if isinstance(af, list) and af:
            af_names = [
                f.get("name", "") for f in af[:2]
                if isinstance(f, dict) and f.get("name")
            ]
            if af_names:
                context_snippets.append("异常/边界场景: " + ", ".join(af_names))

        ac = srr.get("acceptance_criteria", []) or []
        if isinstance(ac, list) and ac:
            ac_texts = [
                a.get("description", "") if isinstance(a, dict) else str(a)
                for a in ac[:3]
            ]
            context_snippets.append("验收标准: " + "; ".join(ac_texts))

        br = srr.get("business_rules", []) or []
        if isinstance(br, list) and br:
            br_texts = [
                b.get("rule", str(b)) if isinstance(b, dict) else str(b)
                for b in br[:3]
            ]
            context_snippets.append("业务规则: " + "; ".join(br_texts))

        # 构建缺口列表
        gaps: list[dict] = []
        for m in completeness.get("critical_missing", []) + completeness.get("important_missing", []):
            gaps.append({
                "field": m["field"],
                "描述": m.get("description", m["field"]),
                "priority": "critical" if m in completeness.get("critical_missing", []) else "important",
                "_is_baseline": False,
            })

        if is_incremental:
            baseline_gaps = srr.get("_baseline_gaps")
            if not isinstance(baseline_gaps, list) or not baseline_gaps:
                baseline_gaps = [
                    "账号密码登录主流程", "密码复杂度与长度规则",
                    "登录失败锁定策略", "登录错误提示规范", "验证码发送与校验规则",
                ]
            for g in baseline_gaps:
                gaps.append({
                    "field": f"baseline.{g}",
                    "描述": str(g),
                    "priority": "important",
                    "_is_baseline": True,
                    "_gap_keys": [
                        "happy_path", "alternate_flows", "data_constraints",
                        "business_rules", "preconditions",
                    ],
                })

        return (
            f"【原始需求文档（最权威参考，请优先据此判断缺口是否已覆盖）】\n"
            f"{srr.get('_raw_text', '')[:3000]}\n"
            f"\n"
            f"【当期需求上下文（SRR 草稿）】\n"
            f"- 标题: {title}\n"
            f"- 摘要: {summary}\n"
            f"{chr(10).join(context_snippets) if context_snippets else '（草稿SRR中暂无可提取的上下文特征）'}\n"
            f"\n"
            f"【待补全的信息缺口（逐一判断是否需要查知识库）】\n"
            f"{json.dumps(gaps, ensure_ascii=False, indent=2) if gaps else '（无缺口）'}\n"
            f"\n"
            f"‼️ 核心规则：逐条审阅每个缺口。先在「原始需求文档」中搜索该缺口的关键信息——"
            f"只要原始文档中明确描述过（不限措辞一致，语义相同即可），则该缺口已被覆盖，**跳过不生成查询**。\n"
            f"如果原始文档没有覆盖，再检查「当期需求上下文」的 SRR 中是否有——"
            f"SRR 已覆盖的也跳过。\n"
            f"只对原始文档和 SRR 都未覆盖的缺口生成自然语言查询（以「？」结尾，8-25字）。\n"
            f"宁可漏查也不要重复提问——不确定是否覆盖的，一律跳过。\n"
            f"仅输出 JSON，不要包含其他解释。"
        )

    async def _generate_kb_queries(
        self, srr: dict, completeness: dict, is_incremental: bool
    ) -> list[dict]:
        """用 LLM 动态生成上下文感知的 KB 搜索查询。

        LLM 失败时直接抛异常上报，不使用固定模板降级。
        """
        gaps_count = (
            len(completeness.get("critical_missing", []))
            + len(completeness.get("important_missing", []))
        )
        if is_incremental:
            baseline = srr.get("_baseline_gaps")
            if isinstance(baseline, list) and baseline:
                gaps_count += len(baseline)
            else:
                gaps_count += 5  # 默认 baseline_gaps 数量

        if gaps_count == 0:
            return []

        user_prompt = self._build_kb_query_user_prompt(srr, completeness, is_incremental)

        try:
            result = await self._llm.call_json(
                prompt=user_prompt,
                system_prompt=prompts.KB_QUERY_GENERATOR_SYSTEM_PROMPT,
                schema_hint={"queries": list},
                temperature=0.3,
            )
            queries = result.get("queries", [])
            if queries:
                logger.info(
                    "[ClarifyAgent] LLM 生成 KB 查询: %d 条（上下文感知）",
                    len(queries),
                )
                # 构建合法 field 集合（防止 LLM 编造列表中不存在的缺口）
                valid_fields: set[str] = set()
                for m in completeness.get("critical_missing", []) + completeness.get("important_missing", []):
                    valid_fields.add(m["field"])
                if is_incremental:
                    bg = srr.get("_baseline_gaps")
                    if isinstance(bg, list) and bg:
                        for g in bg:
                            valid_fields.add(f"baseline.{g}")
                    else:
                        for g in ["账号密码登录主流程", "密码复杂度与长度规则", "登录失败锁定策略",
                                   "登录错误提示规范", "验证码发送与校验规则"]:
                            valid_fields.add(f"baseline.{g}")

                validated: list[dict] = []
                for q in queries:
                    if not isinstance(q, dict) or not q.get("question"):
                        continue
                    field = q.get("field", "")
                    if field and field not in valid_fields:
                        logger.info(
                            "[ClarifyAgent] 丢弃编造查询: field=%s (不在缺口列表中)",
                            field,
                        )
                        continue
                    validated.append({
                        "field": q.get("field", ""),
                        "missing_desc": q.get(
                            "missing_desc",
                            f"LLM生成的查询: {q.get('field','')}",
                        ),
                        "priority": q.get("priority", "important"),
                        "question": q["question"],
                        "_is_baseline": q.get("_is_baseline", False),
                        "_gap_keys": (
                            q.get("_gap_keys", []) if q.get("_is_baseline") else None
                        ),
                    })
                return validated
            return []

        except Exception as e:
            logger.error(
                "[ClarifyAgent] LLM KB 查询生成失败: %s",
                str(e)[:300],
            )
            raise

    # ========================================================================
    @staticmethod
    def _extract_baseline_block(src_srr: dict, q: dict) -> dict:
        """从知识库历史需求的 SRR 中提取基础上下文相关维度块。

        用于增量文档基线补全：不按单一 field 路径取值（历史需求无 baseline.* 字段），
        而是整体提取 happy_path / alternate_flows / data_constraints / business_rules /
        preconditions 等基础维度，供合并进当前增量 SRR。
        """
        keys = q.get("_gap_keys") or ["happy_path", "alternate_flows", "data_constraints", "business_rules", "preconditions"]
        block: dict = {}
        flows = src_srr.get("flows", {}) or {}
        for k in keys:
            if k == "happy_path":
                v = flows.get("happy_path")
            elif k == "alternate_flows":
                v = flows.get("alternate_flows")
            else:
                v = src_srr.get(k)
            if v:
                block[k] = v
        return block

    async def _query_kb_for_answers(
        self,
        session,
        srr: dict,
        questions: list[dict],
        all_similar_reqs: list[dict],
        sqlite_context: dict,
    ) -> list[dict]:
        """对每个问题查询知识库并尝试获取答案。

        流程:
        1. 用问题文本搜索 ChromaDB → 相似需求
        2. 用 SQLite 关联查询 → 模块依赖、测试用例、追溯矩阵
        3. 从搜索结果中提取答案
        4. 记录 Q&A 对

        Args:
            session: SQLite 会话
            srr: 当前需求 SRR
            questions: _formulate_kb_questions 生成的问题列表
            all_similar_reqs: 全局搜索的相似需求（复用）
            sqlite_context: SQLite 增强上下文

        Returns:
            list[dict]: [{question, answer, answered, sources, field, priority, auto_filled}, ...]
        """
        answers = []

        for q in questions:
            # 歧义问题跳过 KB 查询，直接标记为未回答
            if q.get("_skip_kb"):
                answers.append({
                    "id": f"q_{q['field'].replace('.', '_')}",
                    "field": q["field"],
                    "missing_desc": q.get("missing_desc", ""),
                    "context": q.get("missing_desc", ""),
                    "priority": q.get("priority", "important"),
                    "question": q["question"],
                    "answer": "歧义问题需要人工确认，知识库无法直接回答",
                    "answered": False,
                    "sources": [],
                    "auto_filled": False,
                    "input_type": "text",
                    "options": [],
                    "default_answer": q.get("missing_desc", ""),
                })
                continue

            # 搜索相关需求
            try:
                matched = await asyncio.to_thread(
                    vector_store.search_similar_requirements,
                    query=q["question"], top_k=3,
                    product_id=srr.get("requirement", {}).get("product_id", ""),
                )
            except Exception:
                matched = []

            # 从匹配需求中提取答案
            answer_text = ""
            sources = []
            for m in matched:
                if m.get("similarity", 0) < 0.65:
                    continue
                doc = m.get("document", "") or ""
                meta = m.get("metadata", {}) or {}
                req_id = meta.get("req_id", m.get("id", ""))
                srr_json_str = meta.get("srr_json", "")

                # 尝试从 SRR 中提取答案
                field_val = None
                baseline_block = None
                if srr_json_str:
                    try:
                        src_srr = json.loads(srr_json_str)
                        if q.get("_is_baseline"):
                            # 基线问题：提取知识库历史需求的基础上下文相关维度（整个块）
                            baseline_block = self._extract_baseline_block(src_srr, q)
                        else:
                            field_val = self._get_nested_value(src_srr, q["field"])
                    except Exception:
                        pass

                if baseline_block:
                    answer_text += (
                        f"• {req_id}（相似度 {m.get('similarity',0):.2f}）："
                        f"已检索到基础上下文（happy_path/data_constraints/business_rules 等）\n"
                    )
                    sources.append({
                        "id": req_id,
                        "similarity": round(m.get("similarity", 0), 3),
                        "field": q["field"],
                        "value": baseline_block,
                    })
                elif field_val:
                    answer_text += f"• {req_id}（相似度 {m.get('similarity',0):.2f}）：{str(field_val)[:300]}\n"
                    # ⚠️ 保留结构化原始值（不转字符串、不截断），供 _apply_kb_answers
                    # 写回 SRR 时维持字段类型；仅 answer_text 用于展示时截断。
                    sources.append({
                        "id": req_id,
                        "similarity": round(m.get("similarity", 0), 3),
                        "field": q["field"],
                        "value": field_val,
                    })

            answered = len(sources) > 0
            answers.append({
                "id": f"q_{q['field'].replace('.', '_')}",
                "field": q["field"],
                "missing_desc": q.get("missing_desc", ""),
                "context": q.get("missing_desc", ""),
                "priority": q.get("priority", "important"),
                "question": q["question"],
                "answer": answer_text.strip() if answered else "知识库中未找到相关信息，需要人工补充",
                "answered": answered,
                "sources": sources,
                "auto_filled": False,
                "input_type": "text",
                "options": [],
                "default_answer": q.get("missing_desc", ""),
            })

        # v2.11：LLM 校验检索结果质量（判断相关性、拒绝不相关答案、提取具体信息）
        answers = await self._verify_kb_answers(srr, answers)

        return answers

    async def _verify_kb_answers(
        self, srr: dict, answers: list[dict]
    ) -> list[dict]:
        """LLM 校验 KB 检索结果：判断是否真正相关、提取具体答案、拒绝不相关。

        知识问答页之所以能给出好答案，是因为 LLM 读取了检索文档全文后再作答。
        但 clarify_agent 的 KB 查询目前是机械提取字段值——不分析语义相关性。
        本方法补上这一步：将初步有来源的答案送 LLM 审核，不相关的直接拒收。
        """
        # 只校验有来源的答案，无来源的（未命中）不需要 LLM 再判
        candidate_indices = [i for i, a in enumerate(answers) if a.get("answered")]
        if not candidate_indices:
            return answers

        # 构建校验请求：每个候选答案的查询 + 检索到的文档内容
        req_title = (srr.get("requirement", {}) or {}).get("title", "当前需求")
        req_summary = (srr.get("requirement", {}) or {}).get("summary", "")
        verification_items: list[dict] = []
        for idx in candidate_indices:
            a = answers[idx]
            docs: list[str] = []
            for s in a.get("sources", [])[:3]:
                v = s.get("value")
                if isinstance(v, (dict, list)):
                    docs.append(json.dumps(v, ensure_ascii=False)[:600])
                elif v:
                    docs.append(str(v)[:600])
            verification_items.append({
                "index": idx,
                "question": a["question"],
                "documents": docs,
            })

        user_prompt = (
            f"【当期需求】\n"
            f"标题: {req_title}\n"
            f"摘要: {req_summary}\n"
            f"\n"
            f"【待审核的查询与检索结果】\n"
            f"{json.dumps(verification_items, ensure_ascii=False, indent=2)}\n"
            f"\n"
            f"请逐条审核：检索到的文档内容是否真正与查询相关、且在当期需求范围内。\n"
            f"仅输出 JSON，不要包含其他解释。"
        )

        try:
            result = await self._llm.call_json(
                prompt=user_prompt,
                system_prompt=prompts.KB_ANSWER_VERIFIER_SYSTEM_PROMPT,
                schema_hint={"verifications": list},
                temperature=0.1,
            )
            verifications = result.get("verifications", []) or []
            for v in verifications:
                idx = v.get("index")
                if idx is None or idx >= len(answers):
                    continue
                if v.get("relevant"):
                    # LLM 确认相关：用 LLM 提取的答案替换机械答案
                    extracted = v.get("extracted_answer", "")
                    if extracted:
                        answers[idx]["answer"] = extracted
                    # 保留 answered=True
                else:
                    # LLM 判定不相关：拒收
                    logger.info(
                        "[ClarifyAgent] KB 答案校验拒绝: query=%s reason=%s",
                        answers[idx].get("question", "")[:80],
                        v.get("reason", "")[:100],
                    )
                    answers[idx]["answered"] = False
                    answers[idx]["answer"] = (
                        f"知识库检索结果不相关，已拒绝: {v.get('reason', '')}"
                    )
                    answers[idx]["sources"] = []
                    answers[idx]["auto_filled"] = False

        except Exception as e:
            logger.error(
                "[ClarifyAgent] KB 答案校验 LLM 调用失败，保留原始答案: %s",
                str(e)[:200],
            )
            # LLM 失败时不改变答案（降级为原有机械答案，避免全丢弃）

        return answers

    def _apply_kb_answers(
        self, srr: dict, kb_answers: list[dict]
    ) -> tuple[dict, list[dict]]:
        """将 KB 答案应用到 SRR 中。

        Args:
            srr: 当前 SRR（已拷贝）
            kb_answers: _query_kb_for_answers 返回的答案列表

        Returns:
            tuple[dict, list[dict]]: (更新后的 SRR, 补全记录列表)
        """
        augmentations = []
        for a in kb_answers:
            if not a["answered"] or not a["sources"]:
                continue
            # 取最高相似度的答案来源
            best = max(a["sources"], key=lambda s: s.get("similarity", 0))
            field = a["field"]
            value = best.get("value", "")

            if not value or field.startswith("ambiguity."):
                continue  # 歧义问题不自动补全，仍需人工确认

            # 增量文档基线补全：把知识库历史需求的基础上下文合并进 SRR，
            # 不覆盖文档自身已描述的内容（追加到 _baseline_context + additional_details）。
            if a.get("_is_baseline") and isinstance(value, dict) and value:
                existing = srr.get("_baseline_context")
                existing = existing if isinstance(existing, dict) else {}
                merged = dict(value)
                for k, v in existing.items():
                    merged.setdefault(k, v)
                srr["_baseline_context"] = merged
                add = srr.get("additional_details", "") or ""
                base_txt = "\n\n【来自知识库历史版本补全的基础上下文】\n" + json.dumps(value, ensure_ascii=False)[:1500]
                srr["additional_details"] = add + base_txt
                a["auto_filled"] = True
                augmentations.append({
                    "field": field,
                    "source_req_id": best["id"],
                    "similarity": best["similarity"],
                    "value": value,
                    "_is_baseline": True,
                })
                continue

            try:
                self._set_nested_value(srr, field, value)
                a["auto_filled"] = True
                augmentations.append({
                    "field": field,
                    "source_req_id": best["id"],
                    "similarity": best["similarity"],
                    "value": value,
                })
            except Exception:
                pass

        return srr, augmentations

    # ========================================================================
    # KB 自动补全（docs/01 §4.4）— 旧版，保留兼容
    # ========================================================================

    def _kb_augment(
        self,
        srr: dict,
        completeness: dict,
        similar_reqs: list[dict],
    ) -> tuple[dict, list[dict]]:
        """对缺失字段尝试从 KB 自动补全。

        对应 docs/01 §4.4.2 的 kb_augment_requirement 函数。
        策略：缺失字段 → 检索同类需求的对应字段 → 相似度 > 0.85 → 自动填充。

        Args:
            srr: SRR JSON 字典
            completeness: 完整性检测结果
            similar_reqs: KB 检索到的相似需求列表

        Returns:
            tuple[dict, list[dict]]: (补全后的 SRR, 补全记录列表)
        """
        augmented_srr = copy.deepcopy(srr)
        augmentations: list[dict] = []

        if not similar_reqs:
            return augmented_srr, augmentations

        # v2.1: 对 critical + important 缺失字段都尝试 KB 补全
        # critical 字段（如 happy_path / data_constraints）从相似需求补全价值更高
        missing_fields = (
            completeness.get("critical_missing", [])
            + completeness.get("important_missing", [])
        )
        for missing in missing_fields:
            field = missing["field"]
            for req in similar_reqs:
                if req.get("similarity", 0) < 0.85:
                    continue
                # 尝试从相似需求中获取该字段值
                # 优先从 metadata.srr_json（序列化后的完整 SRR）解析
                source_srr = {}
                srr_json_str = req.get("metadata", {}).get("srr_json", "")
                if srr_json_str:
                    try:
                        source_srr = json.loads(srr_json_str)
                    except Exception:
                        source_srr = {}
                source_value = self._get_nested_value(source_srr, field)
                if not source_value:
                    continue
                # 填充字段
                self._set_nested_value(augmented_srr, field, source_value)
                augmentations.append({
                    "field": field,
                    "source_req_id": req.get("id", ""),
                    "source": req.get("id", ""),  # 兼容旧字段名
                    "similarity": req.get("similarity", 0),
                    "value": source_value,
                })
                break  # 该字段已补全，处理下一个

        return augmented_srr, augmentations

    # ========================================================================
    # SQLite 关系型数据增强（v2.6）
    # ========================================================================

    async def _enrich_with_sqlite(
        self, session, srr: dict, similar_reqs: list[dict]
    ) -> dict:
        """从 SQLite 关系型数据补充上下文，增强 KB 补全效果。

        利用产品/项目/模块/标签/依赖图谱/追溯矩阵等关系信息，
        为 KB 自动补全和 LLM 问题生成提供更丰富的上下文。

        Args:
            session: SQLAlchemy AsyncSession
            srr: 当前需求的 SRR
            similar_reqs: ChromaDB 检索到的相似需求列表

        Returns:
            dict: {
                "same_project_reqs": [...],      # 同项目下的其他需求 ID
                "same_module_reqs": [...],        # 同模块下的其他需求 ID
                "shared_tags": [...],             # 共享标签
                "module_deps": [...],              # 模块依赖链
                "existing_traces": [...],          # 已有追溯矩阵条目
                "product_context": {...},           # 产品层级上下文
            }
        """
        from sqlalchemy import select
        from app.db.models import (
            Requirement, TestCase, Module, ModuleDependency,
            Traceability, Project,
        )

        context: dict = {
            "same_project_reqs": [],
            "same_module_reqs": [],
            "shared_tags": [],
            "module_deps": [],
            "existing_traces": [],
            "product_context": {},
        }

        current_req_id = srr.get("requirement", {}).get("id", "")
        current_modules = srr.get("requirement", {}).get("modules", []) or []
        product_id = srr.get("requirement", {}).get("product_id", "")

        # 1. 查询当前需求的 SQLite 记录，获取 project_id 和 tags
        try:
            stmt = select(Requirement).where(Requirement.id == current_req_id)
            result = await session.execute(stmt)
            current_record = result.scalar_one_or_none()
        except Exception:
            current_record = None

        project_id = current_record.project_id if current_record else None

        # 2. 同项目下的其他需求
        if project_id:
            try:
                stmt = select(Requirement.id, Requirement.title, Requirement.priority).where(
                    Requirement.project_id == project_id,
                    Requirement.id != current_req_id,
                ).limit(10)
                rows = (await session.execute(stmt)).all()
                context["same_project_reqs"] = [
                    {"id": r[0], "title": r[1], "priority": r[2]} for r in rows
                ]
            except Exception:
                pass

        # 3. 同模块下的其他需求（用 modules JSON 字段匹配）
        if current_modules:
            try:
                # 查询所有需求，在 Python 中做模块交集匹配
                stmt = select(Requirement.id, Requirement.title, Requirement.modules).where(
                    Requirement.id != current_req_id,
                    Requirement.product_id == product_id,
                ).limit(50)
                rows = (await session.execute(stmt)).all()
                for r in rows:
                    other_modules = set(r[2] or [])
                    if other_modules & set(current_modules):
                        context["same_module_reqs"].append({
                            "id": r[0], "title": r[1],
                            "shared_modules": list(other_modules & set(current_modules)),
                        })
                # 仅保留前10条
                context["same_module_reqs"] = context["same_module_reqs"][:10]
            except Exception:
                pass

        # 4. 共享标签
        if current_record and current_record.tags:
            try:
                stmt = select(Requirement.id, Requirement.title, Requirement.tags).where(
                    Requirement.id != current_req_id,
                    Requirement.product_id == product_id,
                ).limit(50)
                rows = (await session.execute(stmt)).all()
                for r in rows:
                    other_tags = set(r[2] or [])
                    shared = other_tags & set(current_record.tags)
                    if shared:
                        context["shared_tags"].append({
                            "id": r[0], "title": r[1], "shared_tags": list(shared),
                        })
                context["shared_tags"] = context["shared_tags"][:10]
            except Exception:
                pass

        # 5. 模块依赖链（找出当前模块的上下游依赖）
        if current_modules:
            try:
                stmt = select(ModuleDependency).where(
                    ModuleDependency.source_module_id.in_(current_modules)
                    | ModuleDependency.target_module_id.in_(current_modules)
                ).limit(20)
                deps = (await session.execute(stmt)).scalars().all()
                context["module_deps"] = [
                    {
                        "source": d.source_module_id,
                        "target": d.target_module_id,
                        "relation_type": d.relation_type,
                        "strength": d.strength,
                    }
                    for d in deps
                ]
            except Exception:
                pass

        # 6. 已有的追溯矩阵条目（检索历史用例与需求的关联）
        try:
            stmt = select(Traceability).limit(50)
            traces = (await session.execute(stmt)).scalars().all()
            context["existing_traces"] = [
                {"test_case_id": t.test_case_id, "requirement_id": t.requirement_id,
                 "trace_type": t.trace_type, "confidence": t.confidence}
                for t in traces
            ]
        except Exception:
            pass

        # 7. 产品上下文
        try:
            from app.db.models import Product
            stmt = select(Product).where(Product.id == product_id)
            prod = (await session.execute(stmt)).scalar_one_or_none()
            if prod:
                context["product_context"] = {
                    "id": prod.id, "name": prod.name, "description": prod.description,
                }
        except Exception:
            pass

        return context

    # ========================================================================
    # LLM 问题生成（docs/01 §4.5）
    # ========================================================================

    async def _generate_questions(
        self,
        srr: dict,
        completeness: dict,
        ambiguities: list[dict],
        similar_reqs: list[dict],
        augmentations: list[dict],
        sqlite_context: dict | None = None,
    ) -> dict:
        """调用 LLM 生成针对性澄清问题。

        对应 docs/01 §4.5 的 generate_clarification_questions_batch 函数。
        仅针对 KB 无法解决的缺口生成问题。

        Args:
            srr: SRR JSON 字典（已 KB 补全）
            completeness: 完整性检测结果
            ambiguities: 歧义检测结果
            similar_reqs: 同类需求列表
            augmentations: KB 已补全的字段列表
            sqlite_context: SQLite 关系型增强上下文（v2.6）

        Returns:
            dict: {issues_detected: [...], questions: [...], quality_report: {...}}
        """
        # 准备 KB 已补全字段信息（告知 LLM 不要再问这些字段）
        kb_inferred_fields = [
            {"field": a["field"], "value": str(a["value"]), "similarity": a["similarity"]}
            for a in augmentations
        ]

        # 准备同类需求维度信息（辅助遗漏检测）
        similar_reqs_hint = [
            {"title": r.get("metadata", {}).get("req_id", ""), "similarity": r.get("similarity", 0)}
            for r in similar_reqs[:3]
        ]

        user_prompt = prompts.build_user_prompt(
            srr=srr,
            kb_inferred_fields=kb_inferred_fields or None,
            similar_reqs=similar_reqs_hint or None,
        )

        try:
            result = await self._llm.call_json(
                prompt=user_prompt,
                system_prompt=prompt_manager.get("clarify_agent", "SYSTEM_PROMPT"),
                schema_hint={"questions": list, "quality_report": dict},
                temperature=0.2,
            )
            # 后处理：仅过滤掉 optional 问题（保留全部 critical/important，不截断）
            questions = result.get("questions", [])
            questions = [q for q in questions if q.get("priority") != "optional"]
            result["questions"] = questions
            return result
        except Exception as e:
            logger.error(
                "[ClarifyAgent] LLM 问题生成失败: %s",
                str(e)[:300],
            )
            raise

    def _ensure_critical_questions(
        self,
        llm_questions: list[dict],
        unresolved_qa: list[dict],
        completeness: dict,
    ) -> list[dict]:
        """保证 critical 字段缺失一定被问（规则层强制守卫）。

        方向A（v2.9）：LLM 主导问题生成，但 critical 字段（summary /
        acceptance_criteria）缺失会直接阻断 SRR 生成，必须兜底确保被问到，
        避免 LLM 漏判导致需求缺口漏网。important / 语义遗漏 / 歧义完全由 LLM 决定。
        """
        critical_fields = {m["field"] for m in completeness.get("critical_missing", [])}
        llm_fields = {q.get("field") for q in llm_questions}
        guarded = list(llm_questions)
        for qa in unresolved_qa:
            if qa.get("field") in critical_fields and qa.get("field") not in llm_fields:
                guarded.append(qa)
                logger.debug("[ClarifyAgent] critical 守卫补回问题: field={}", qa.get("field"))
        return guarded

    # ========================================================================
    # 质量评分计算
    # ========================================================================

    def _calc_quality_score(
        self,
        completeness: dict,
        ambiguities: list[dict],
        llm_result: dict,
    ) -> float:
        """计算质量评分。

        评分公式（docs/01 §4.3）：
            overall = completeness_score × 0.6 + (1 - ambiguity_score) × 0.4

        Args:
            completeness: 完整性检测结果
            ambiguities: 歧义检测结果
            llm_result: LLM 输出（可能含 quality_report）

        Returns:
            float: 质量评分 0.0-1.0
        """
        # 优先使用 LLM 输出的评分，并截断到 [0, 1]
        llm_report = llm_result.get("quality_report", {})
        if isinstance(llm_report, dict) and "overall_score" in llm_report:
            return min(max(float(llm_report["overall_score"]), 0.0), 1.0)

        # 无 LLM 质量报告时，基于规则做近似估算（仅用于评分，不影响生成质量）
        completeness_score = completeness.get("completeness_score", 0)
        ambiguity_score = min(1.0, len(ambiguities) * 0.15)
        score = completeness_score * 0.6 + (1 - ambiguity_score) * 0.4
        return min(max(score, 0.0), 1.0)

    # ========================================================================
    # 工具方法
    # ========================================================================

    def _get_nested_value(self, data: dict, path: str) -> Any:
        """安全获取嵌套字段值。"""
        current: Any = data
        for key in path.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(key)
            if current is None:
                return None
        return current

    def _set_nested_value(self, data: dict, path: str, value: Any) -> None:
        """设置嵌套字段值（自动创建中间字典）。"""
        keys = path.split(".")
        current = data
        for key in keys[:-1]:
            if key not in current or not isinstance(current[key], dict):
                current[key] = {}
            current = current[key]
        current[keys[-1]] = value


# 全局单例
clarify_agent = ClarifyAgent()

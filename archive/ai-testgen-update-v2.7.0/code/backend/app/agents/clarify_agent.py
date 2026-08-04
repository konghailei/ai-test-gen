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
import re
import time
from typing import Any

from app.config import settings
from app.core.llm_client import llm_client
from app.db.milvus_store import vector_store
from app.prompts import clarify_agent as prompts
from app.core import prompt_manager
from app.agents.requirement_patterns import pattern_matcher
from app.utils.logger import logger


# ============================================================================
# 字段冲突优先级（docs/01 §4.4.1）
# ============================================================================
# 从高到低：用户原文 > 人工回答 > KB 补全 > 模式默认 > LLM 推断
FIELD_PRIORITY: dict[str, int] = {
    "USER_INPUT": 0,
    "HUMAN_ANSWERED": 1,
    "KB_INFERRED": 2,
    "PATTERN_DEFAULT": 3,
    "LLM_INFERRED": 4,
}


# ============================================================================
# v2.25: 字段到问题的智能映射表 — 降级时生成具体、可回答的澄清问题
# ============================================================================
# 当 LLM 不可用（如配额耗尽）导致 _generate_kb_queries / _refine_unresolved_to_questions
# 降级时，使用此映射表为每个缺口字段生成：
# 1. 具体的问题文案（含需求标题 + 字段业务含义）
# 2. 上下文描述（含功能点信息，帮助用户理解"是哪部分的规则"）
# 3. 默认 input_type（优先 multi_select，让用户多选降低回答成本）
# 4. 默认 options（基于行业最佳实践的标准选项）

FIELD_QUESTION_TEMPLATES: dict[str, dict] = {
    "flows.alternate_flows": {
        "field_desc": "异常/边界流程",
        "question_template": "【{req_title}】缺少异常和边界处理流程，请选择需要覆盖的异常场景（可多选）：",
        "input_type": "multi_select",
        "options": [
            "网络异常/断线重连处理",
            "权限不足/越权访问处理",
            "并发冲突/数据竞争处理",
            "输入越界/非法数据处理",
            "超时/重试机制",
            "资源耗尽/降级处理",
        ],
        "context_template": "当前需求已解析出 {fp_count} 个功能点（{fp_names}），但缺少各功能点的异常/边界流程。这些流程对测试用例的边界覆盖至关重要。",
    },
    "data_constraints": {
        "field_desc": "数据约束/校验规则",
        "question_template": "【{req_title}】缺少数据校验规则，请选择需要覆盖的约束类型（可多选）：",
        "input_type": "multi_select",
        "options": [
            "字符串长度限制（最小/最大长度）",
            "格式校验（邮箱/手机号/URL/日期）",
            "数值范围限制（最小值/最大值）",
            "必填字段校验",
            "唯一性约束（如用户名不可重复）",
            "枚举值限制（如状态只能是 A/B/C）",
        ],
        "context_template": "当前需求涉及 {fp_count} 个功能点，但未明确数据约束。数据校验规则是生成边界测试用例的关键依据。",
    },
    "business_rules": {
        "field_desc": "核心业务规则",
        "question_template": "【{req_title}】缺少核心业务规则，请选择需要补充的规则类型（可多选）：",
        "input_type": "multi_select",
        "options": [
            "权限控制规则（谁能做什么）",
            "状态流转规则（状态机）",
            "计算/计费规则",
            "触发条件与通知规则",
            "数据可见性规则",
            "审批流程规则",
        ],
        "context_template": "当前需求已解析出 {fp_count} 个功能点，但缺少业务规则定义。业务规则是测试用例逻辑覆盖的核心依据。",
    },
    "flows.happy_path": {
        "field_desc": "正常操作流程",
        "question_template": "【{req_title}】缺少正常操作流程，请描述用户从开始到完成的主要步骤：",
        "input_type": "text",
        "options": [],
        "context_template": "当前需求已解析出 {fp_count} 个功能点，但缺少正常流程描述。",
    },
    "acceptance_criteria": {
        "field_desc": "验收标准",
        "question_template": "【{req_title}】缺少验收标准，请补充功能上线时需要满足的可验证条件：",
        "input_type": "text",
        "options": [],
        "context_template": "当前需求缺少验收标准，这将影响测试通过的判定。",
    },
    "requirement.summary": {
        "field_desc": "功能概述",
        "question_template": "【{req_title}】缺少功能概述，请用一段话描述产品做什么、面向谁、解决什么问题：",
        "input_type": "text",
        "options": [],
        "context_template": "当前需求缺少功能概述。",
    },
}


def _build_field_question(field: str, description: str, srr: dict) -> dict:
    """根据字段路径和 SRR 上下文，生成具体的澄清问题。

    v2.25: 降级路径专用 — 当 LLM 不可用时，使用 FIELD_QUESTION_TEMPLATES
    生成包含需求标题、功能点信息、多选选项的具体问题。

    Args:
        field: 字段路径（如 "flows.alternate_flows"）
        description: 字段描述（来自 COMPLETENESS_CHECKLIST）
        srr: 当前 SRR，用于提取需求标题和功能点信息

    Returns:
        dict: 包含 question/context/input_type/options 的字典
    """
    req_info = srr.get("requirement", {}) or {}
    req_title = req_info.get("title", "当前需求") or "当前需求"

    # 提取功能点信息
    fp_summary = srr.get("fp_summary", []) or []
    fp_count = len(fp_summary)
    fp_names = "、".join([fp.get("name", "") for fp in fp_summary[:3] if fp.get("name")])
    if fp_count > 3:
        fp_names += f" 等{fp_count}个功能点"

    template = FIELD_QUESTION_TEMPLATES.get(field, {})
    if template:
        question = template["question_template"].format(req_title=req_title)
        context = template["context_template"].format(
            fp_count=fp_count, fp_names=fp_names or "未识别",
        )
        return {
            "question": question,
            "context": context,
            "input_type": template["input_type"],
            "options": list(template["options"]),
            "default_answer": "",
        }

    # 未知字段：使用 description 生成通用问题
    return {
        "question": f"【{req_title}】缺少{description}，请补充具体内容：",
        "context": f"当前需求已解析出 {fp_count} 个功能点，但缺少{description}。",
        "input_type": "text",
        "options": [],
        "default_answer": "",
    }


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

    @staticmethod
    def _dedup_questions(questions: list[dict], stage: str = "") -> list[dict]:
        """问题列表去重（v2.31 新增）。

        作为最终安全网，确保返回给用户的问题列表绝不重复。
        去重策略（双重）：
        1. 按 field+question 全文去重（相同 field 且相同 question 只保留第一条）
        2. 按 id 去重（相同 id 只保留第一条）

        Args:
            questions: 待去重的问题列表
            stage: 调用阶段标识（用于日志，如 "after_refine" / "final"）

        Returns:
            list[dict]: 去重后的问题列表
        """
        if not questions:
            return questions

        deduped: list[dict] = []
        seen_q_keys: set[str] = set()  # field+question 全文去重
        seen_ids: set[str] = set()     # id 去重
        skipped = 0

        for q in questions:
            field = q.get("field", "")
            question_text = (q.get("question", "") or "").strip()
            qid = q.get("id", "")

            # 双重去重：field+question 全文 + id
            q_key = f"{field}::{question_text}" if question_text else f"__no_q__::{field}"
            if q_key in seen_q_keys:
                skipped += 1
                logger.info(
                    "[ClarifyAgent] 去重({}) 跳过重复问题: field={!r} question={!r}",
                    stage, field, question_text[:60],
                )
                continue
            if qid and qid in seen_ids:
                skipped += 1
                logger.info(
                    "[ClarifyAgent] 去重({}) 跳过重复 id: id={} field={!r}",
                    stage, qid, field,
                )
                continue

            seen_q_keys.add(q_key)
            if qid:
                seen_ids.add(qid)
            deduped.append(q)

        if skipped > 0:
            logger.info(
                "[ClarifyAgent] 去重({}) 完成: 原始 {} 条 → 去重后 {} 条（跳过 {} 条）",
                stage, len(questions), len(deduped), skipped,
            )
        return deduped

    async def clarify(self, srr: dict, session = None) -> dict:
        """对 SRR 执行需求澄清。

        v2.52: 不再长持有外部 session，内部 DB 查询使用短生命周期 session，
        避免 LLM 调用期间占用连接池。

        Args:
            srr: SRR JSON 字典
            session: 已废弃（保留签名兼容性，不再使用）

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

        # 读取知识库专属 LLM 供应商（若已设置），KB 相关 LLM 调用使用该供应商
        # v2.52: 使用内部短生命周期 session，不长持有外部连接
        kb_provider: str | None = None
        try:
            from app.api.knowledge import get_kb_llm_provider
            from app.db.database import async_session as _as
            async with _as() as s:
                kb_provider = await get_kb_llm_provider(s)
        except Exception as e:
            logger.warning("[ClarifyAgent] 读取 KB LLM 供应商失败，回退全局主 LLM: {}", str(e)[:120])

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
        # v2.54: 先用 LLM 判断是否为优化需求（替代关键词匹配），结果存入 srr 供
        # _check_completeness 和 _detect_optimization_impact 共用，避免重复调用
        await self._llm_detect_optimization(srr, kb_provider=kb_provider)
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

        # ②b 需求模式库匹配（docs/01 §4.3.3 遗漏检测 + §6 需求模式库）
        # 对比同类需求模式，发现当前需求缺失的维度，作为规则层遗漏检测的补充。
        # KB 查询前执行，让 KB 查询能覆盖模式缺失维度。
        pattern_context: dict | None = None
        try:
            matched_pattern = pattern_matcher.match_pattern(srr)
            if matched_pattern:
                missing_dims = pattern_matcher.detect_missing_dimensions(srr, matched_pattern)
                if missing_dims:
                    # 将缺失维度加入 important_missing，供 KB 查询和 LLM 问题生成覆盖
                    for dim in missing_dims:
                        completeness["important_missing"].append({
                            "field": f"pattern_gap.{dim}",
                            "description": f"同类需求（{matched_pattern['name']}）普遍包含但当前缺失：{dim}",
                        })
                    logger.info("[ClarifyAgent] 模式库遗漏检测: pattern={}, missing_dims={}",
                                matched_pattern["pattern_id"], missing_dims)
                pattern_context = {
                    "pattern_id": matched_pattern["pattern_id"],
                    "pattern_name": matched_pattern["name"],
                    "common_boundary": matched_pattern["common_boundary"],
                    "risk_points": matched_pattern["risk_points"],
                    "missing_dimensions": missing_dims,
                }
        except Exception as e:
            logger.warning("[ClarifyAgent] 需求模式库匹配异常（已降级跳过）: {}", str(e)[:200])

        # ③ KB 检索：以每个缺失字段和歧义点为问题，向知识库提问
        import time, copy
        augmented_srr = copy.deepcopy(srr)

        # ③a 用需求摘要做全局搜索，供 PG 增强和问题查询复用
        summary = srr.get("requirement", {}).get("summary", "")
        _t_sim = time.time()
        global_similar = await asyncio.to_thread(self._search_similar_requirements, srr)
        _d_sim = int((time.time() - _t_sim) * 1000)

        # ③b PG 关系增强（先跑，供后续 Q&A 查询使用）
        # v2.52: 使用内部短生命周期 session，不长持有外部连接
        _t_sql = time.time()
        try:
            from app.db.database import async_session as _as
            async with _as() as pg_sess:
                pg_context = await self._enrich_with_pg(pg_sess, srr, global_similar)
        except Exception as e:
            logger.warning("[ClarifyAgent] PG 关系增强失败（降级跳过）: {}", str(e)[:150])
            pg_context = {}
        _d_sql = int((time.time() - _t_sql) * 1000)

        _t3 = time.time()
        # ③ KB 查询生成（v2.10）：LLM 动态生成上下文感知的搜索查询。
        #    不再使用固定模板，LLM 失败时直接抛异常上报，确保查询质量。
        #    注入原始需求全文，让 KB 查询生成器能参考原始文档，避免对已有内容的重复提问。
        # v2.52: 使用局部 raw_text 变量，不污染调用方 srr 字典
        raw_text_for_query = srr.get("_raw_text", "")
        if not raw_text_for_query:
            try:
                from sqlalchemy import select as _select
                from app.db.models import Requirement as _ReqModel
                req_id = srr.get("requirement", {}).get("id", "")
                if req_id:
                    from app.db.database import async_session as _as
                    async with _as() as rt_sess:
                        r = await rt_sess.execute(
                            _select(_ReqModel.raw_text).where(_ReqModel.id == req_id)
                        )
                        raw = r.scalar_one_or_none()
                        if raw:
                            raw_text_for_query = raw
            except Exception:
                pass
        kb_questions = await self._generate_kb_queries(
            srr, completeness, srr.get("_is_incremental", False), kb_provider=kb_provider
        )

        _d3 = int((time.time() - _t3) * 1000)
        logger.info("[ClarifyAgent] 生成 KB 问题: count={}", len(kb_questions))

        # ④ 对每个问题查询 KB + PG，获取答案
        _t4 = time.time()
        kb_answers: list[dict] = await self._query_kb_for_answers(
            srr, kb_questions, global_similar, pg_context,
        )
        _d4 = int((time.time() - _t4) * 1000)
        logger.info("[ClarifyAgent] KB 回答: answered={}, unanswered={}, duration={}ms",
                    sum(1 for a in kb_answers if a.get("answered")),
                    sum(1 for a in kb_answers if not a.get("answered")),
                    _d4)

        # ⑤ 应用 KB 答案 → 自动补全 SRR（含字段冲突处理，docs/01 §4.4.1）
        _t5 = time.time()
        augmented_srr, augmentations, field_conflicts = await self._apply_kb_answers(augmented_srr, kb_answers)
        _d5 = int((time.time() - _t5) * 1000)
        logger.info("[ClarifyAgent] KB 应用: augmentations={}, field_conflicts={}",
                    len(augmentations), len(field_conflicts))

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
                "duration_ms": qa.get("duration_ms", _d4),  # v2.18: 使用单条查询耗时
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
        # v2.25: 使用 _build_field_question 丰富 context/options，即使 LLM 精炼失败也有可用选项
        # v2.29: 按 field+question 双重去重——
        #   - 同 field 且同 question 文本（前40字符）→ 视为重复，只保留一条
        #   - 同 field 但不同 question（不同维度，如"文本长度"vs"文件大小"）→ 保留，生成唯一 id
        #   - 空 field → 按 question 文本去重
        # v2.30: 修复 id 冲突——同 field 的多问题用 _N 后缀区分，避免 _refine 时被 dict 覆盖
        unresolved_qa = [qa for qa in kb_answers if not qa.get("answered")]
        if unresolved_qa:
            seen_keys: set[str] = set()
            field_counters: dict[str, int] = {}  # 每个 field 的问题计数，用于生成唯一 id
            questions = []
            for qa in unresolved_qa:
                field = qa.get("field", "")
                question_text = qa.get("question", "") or ""
                # 双重去重：field + question 前40字符
                dedup_key = f"{field}::{question_text[:40]}" if question_text else field
                if dedup_key in seen_keys:
                    logger.info(
                        "[ClarifyAgent] 跳过重复 KB 未答问题: field={!r} question={!r}",
                        field, question_text[:60],
                    )
                    continue
                seen_keys.add(dedup_key)

                # 生成唯一 id：同 field 的第 N 个问题用 _N 后缀
                field_counters[field] = field_counters.get(field, 0) + 1
                field_slug = field.replace('.', '_') if field else "gap"
                if field_counters[field] == 1:
                    qid = f"kb_{field_slug}"
                else:
                    qid = f"kb_{field_slug}_{field_counters[field]}"

                desc = qa.get("missing_desc", field)
                # v2.25: 使用智能映射表生成 context/input_type/options
                q_data = _build_field_question(field, desc, augmented_srr)
                questions.append({
                    "id": qid,
                    "field": field,
                    "priority": qa.get("priority", "important"),
                    "category": "completeness",
                    # 优先使用 KB 查询时生成的问题（LLM 版），降级时使用模板问题
                    "question": question_text or q_data["question"],
                    "context": q_data["context"],
                    "default_answer": qa.get("missing_desc", ""),
                    "input_type": q_data["input_type"],
                    "options": q_data["options"],
                })
            questions = self._ensure_critical_questions(
                questions, unresolved_qa, completeness, srr=augmented_srr,
            )
            # v2.30: 去重 _ensure_critical_questions 添加的重复问题（按 id 去重）
            seen_ids: set[str] = set()
            deduped: list[dict] = []
            for q in questions:
                qid = q.get("id", "")
                if qid and qid in seen_ids:
                    logger.info("[ClarifyAgent] 跳过 _ensure_critical 重复问题: id={}", qid)
                    continue
                if qid:
                    seen_ids.add(qid)
                deduped.append(q)
            questions = deduped
            # ⭐ 精炼：为 KB 未答转人工的澄清问题补充可选项（input_type/options），
            # 降低用户回答成本。LLM 失败则保留原 text + 空选项降级。
            _t_refine = time.time()
            questions = await self._refine_unresolved_to_questions(questions, augmented_srr, kb_provider=kb_provider)
            _d_refine = int((time.time() - _t_refine) * 1000)
            # v2.31: 精炼后去重——LLM 精炼时可能修改 question 文本导致重复
            questions = self._dedup_questions(questions, stage="after_refine")
        else:
            questions = []

        # ⑧b 需求模式库缺失维度（docs/01 §4.3.3 遗漏检测）
        # 模式库检测出的缺失维度已在步骤②b 加入 completeness["important_missing"]，
        # 并通过步骤③的 LLM KB 查询生成器做了"原文已覆盖则跳过"过滤。
        # 能被 KB 回答的已在步骤⑤补全，不能被 KB 回答的已在步骤⑧转为人工问题。
        # 因此此处不再重复转为问题——避免绕过 LLM 过滤产生误报提问。
        # pattern_context 仍保留用于质量报告和前端展示。
        if pattern_context and pattern_context.get("missing_dimensions"):
            logger.debug("[ClarifyAgent] 模式库缺口已通过KB路径处理: pattern={}, dims={}",
                         pattern_context.get("pattern_id"),
                         pattern_context.get("missing_dimensions"))

        # ⑨ 冷启动深度问题已移除：不再生成固定的模块/边界/依赖问题，
        # 只提问基于需求文档实际分析产生的问题（KB 查询、缺失维度、字段冲突等）。

        # ⑨b 优化需求场景检测（v2.30 新增，v2.32 扩展为多问题）：
        # 如果需求是优化/改进/升级类需求，必须询问"是否影响历史数据"，
        # 并提供文本输入框让用户填写具体影响的历史数据范围，
        # 避免优化需求破坏已有数据或导致历史数据不一致。
        opt_questions = self._detect_optimization_impact(srr)
        if opt_questions:
            questions.extend(opt_questions)
            logger.info(
                "[ClarifyAgent] 检测到优化需求，添加历史数据影响问题: {} 个",
                len(opt_questions),
            )

        # v2.31: 最终全文去重安全网——在所有问题生成完成后，按 field+question 全文去重，
        # 确保无论前面哪个环节遗漏，最终返回给用户的问题列表绝不重复。
        # v2.39: 在去重前先合并逻辑矛盾检测问题（避免被去重逻辑误删）
        contradiction_questions = self._detect_logical_contradictions(srr)
        if contradiction_questions:
            questions.extend(contradiction_questions)
            logger.info(
                "[ClarifyAgent] 逻辑矛盾检测问题已合并: {} 个",
                len(contradiction_questions),
            )
        questions = self._dedup_questions(questions, stage="final")

        # v2.54: 清理临时字段 _llm_is_optimization，避免持久化到数据库 SRR
        srr.pop("_llm_is_optimization", None)
        augmented_srr.pop("_llm_is_optimization", None)

        logger.info("[ClarifyAgent] KB已回答: {}, 待人工澄清(LLM精炼): {}",
                    sum(1 for a in kb_answers if a.get("answered")), len(questions))

        # 计算质量评分
        quality_score = self._calc_quality_score(completeness, ambiguities, {"questions": questions, "quality_report": {}})

        # 判定状态：仍有需人工澄清的问题 → needs_clarification；否则 passed
        # 修正 Bug3：状态严格由「是否存在待人工回答的问题」驱动，
        # 不再因 optional 字段缺失致质量分跌破阈值而误判 needs_clarification（导致 draft 卡死）。
        # 质量门禁(QualityGate)在下游独立把关，clarify 阶段只负责收集必要澄清问题。
        threshold = self.PASS_THRESHOLD
        if questions:
            status = "needs_clarification"
        else:
            status = "passed"

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
            # ⭐ v2.12 新增：字段冲突列表（docs/01 §4.4.1），供前端展示人工确认
            "field_conflicts": field_conflicts,
            # ⭐ v2.24 新增：子阶段耗时分解（供任务管理展示 KB 检索 vs LLM 调用）
            "phase_breakdown": {
                "kb_status_check_ms": _d1 + _d2,
                "kb_similar_search_ms": _d_sim,
                "pg_enrich_ms": _d_sql,
                "llm_kb_query_gen_ms": _d3,
                "kb_qa_ms": _d4,
                "kb_apply_ms": _d5,
                "llm_question_refine_ms": _d_refine if unresolved_qa else 0,
            },
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
        v2.34: 新增 optimization 级别，仅当 legacy_data_impact.is_optimization=True
               时才检查迁移/回滚等专项字段，避免对新建需求误报。

        Args:
            srr: SRR JSON 字典

        Returns:
            dict: {
                "critical_missing": [...],
                "important_missing": [...],
                "optional_missing": [...],
                "optimization_missing": [...],  # v2.34 新增
                "completeness_score": float,
                "passed": bool
            }
        """
        result = {
            "critical_missing": [],
            "important_missing": [],
            "optional_missing": [],
            "optimization_missing": [],
            "completeness_score": 1.0,
            "passed": True,
        }

        # v2.54: 判断是否为优化需求——优先使用 LLM 语义判断结果（srr["_llm_is_optimization"]），
        # 降级到 legacy_data_impact.is_optimization，最后降级到关键词匹配
        ldi = srr.get("legacy_data_impact") or {}
        is_opt = bool(ldi.get("is_optimization")) if isinstance(ldi, dict) else False
        # v2.54: LLM 判断结果优先于 legacy_data_impact.is_optimization
        llm_opt = srr.get("_llm_is_optimization")
        if isinstance(llm_opt, dict):
            is_opt = bool(llm_opt.get("is_optimization", is_opt))
        # v2.53: 用户已明确"不影响历史数据" -> 视为非优化需求，跳过 optimization 缺口检查
        # （避免 _run_clarify_reparse 二次验证时再次报 legacy_data_impact.* 缺失）
        if is_opt and isinstance(ldi, dict):
            impact_scope = str(ldi.get("impact_scope", "") or "")
            user_clarification = str(ldi.get("user_clarification", "") or "")
            no_impact_markers = ("不影响历史数据", "仅新增功能", "无影响")
            if any(m in impact_scope or m in user_clarification for m in no_impact_markers):
                is_opt = False
                logger.info(
                    "[ClarifyAgent] 用户已确认不影响历史数据，completeness 跳过 optimization 检查",
                )
        # v2.54: 关键词仅作为最终兜底（LLM 未返回结果时），且仅匹配强词
        # 弱词（完善/增强/调整等）不再作为判断依据——它们在新功能描述中太常见
        if not is_opt and not isinstance(llm_opt, dict):
            title = (srr.get("requirement") or {}).get("title", "")
            summary = (srr.get("requirement") or {}).get("summary", "")
            detect_text = f"{title}\n{summary}".lower()
            is_opt = any(kw in detect_text for kw in self.OPTIMIZATION_KEYWORDS_STRONG)

        for level, fields in prompts.COMPLETENESS_CHECKLIST.items():
            # v2.34: optimization 级别仅对优化需求检查
            if level == "optimization" and not is_opt:
                continue
            for field, desc in fields.items():
                val = self._get_nested_value(srr, field)
                if not self._has_meaningful_content(field, val):
                    result[f"{level}_missing"].append({"field": field, "description": desc})

        # 评分（optimization_missing 不计入总分，避免非优化需求被误扣）
        missing_weight = (
            len(result["critical_missing"]) * 3
            + len(result["important_missing"]) * 1
            + len(result["optional_missing"]) * 0.3
        )
        # 总分仅统计 critical/important/optional（不含 optimization）
        total = sum(
            len(v) for k, v in prompts.COMPLETENESS_CHECKLIST.items()
            if k != "optimization"
        )
        result["completeness_score"] = max(0.0, 1.0 - missing_weight / total) if total else 1.0
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
            # v2.52: 增加 doc_type="requirement" 过滤，避免 FP 向量混入补全上下文。
            # FP 向量的 srr_json 结构是单功能点 SRR，与总 SRR 结构不同，
            # 混入会导致 KB 补全时注入 FP 级字段值，需求分析粒度错乱。
            return vector_store.search_similar_requirements(
                query=summary, product_id=product_id, modules=modules,
                top_k=5, status="archived", doc_type="requirement",
            )
        except Exception as e:
            logger.warning("[ClarifyAgent] KB 检索相似需求失败（不阻断）: {}", str(e)[:200])
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
    # 导致向量库语义检索命中率偏低，尤其对于增量文档、MFA/SSO 等细分场景。
    # LLM 生成的查询包含功能名、关键特征、技术术语，检索更精准。
    # ========================================================================

    def _build_kb_query_user_prompt(
        self, srr: dict, completeness: dict, is_incremental: bool
    ) -> str:
        """构建 KB 查询生成的用户指令。

        将需求上下文 + 缺口信息组织成 prompt，让 LLM 生成具体、上下文感知的
        查询文本（直接用作向量库语义检索 query），而非固定模板。
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

        # v2.34: 总 SRR 的 business_rules 已迁移到 cross_fp_interactions.shared_business_rules
        # 优先从 shared_business_rules 提取，兼容旧数据中仍存在的顶层 business_rules
        cfi = srr.get("cross_fp_interactions") or {}
        br = cfi.get("shared_business_rules", []) or srr.get("business_rules", []) or []
        if isinstance(br, list) and br:
            br_texts = [
                b.get("rule", str(b)) if isinstance(b, dict) else str(b)
                for b in br[:3]
            ]
            context_snippets.append("跨FP共享规则: " + "; ".join(br_texts))

        # 构建缺口列表
        gaps: list[dict] = []
        # v2.34: 合并 optimization_missing（优化需求专项缺口）
        all_missing = (
            completeness.get("critical_missing", [])
            + completeness.get("important_missing", [])
            + completeness.get("optimization_missing", [])
        )
        for m in all_missing:
            # 判断优先级
            if m in completeness.get("critical_missing", []):
                priority = "critical"
            elif m in completeness.get("optimization_missing", []):
                priority = "optimization"
            else:
                priority = "important"
            gaps.append({
                "field": m["field"],
                "描述": m.get("description", m["field"]),
                "priority": priority,
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
            f"\n"
            f"⚠️ 增量文档特殊规则：如果缺口标记了 _is_baseline=true，说明该缺口是"
            f"增量文档声明需要从历史版本（知识库）补全的基础维度。增量文档可能只是"
            f"「引用」了历史版本（如'请参见V1.0'）但并未包含具体内容——"
            f"**引用不等于覆盖**。对于 _is_baseline=true 的缺口，只要原始文档中没有"
            f"该维度的具体内容描述（仅有引用语句不算），就必须生成 KB 查询。\n"
            f"仅输出 JSON，不要包含其他解释。"
        )

    async def _generate_kb_queries(
        self, srr: dict, completeness: dict, is_incremental: bool,
        kb_provider: str | None = None,
    ) -> list[dict]:
        """用 LLM 动态生成上下文感知的 KB 搜索查询。

        v2.18: LLM 失败时降级为使用缺口 field 名生成简单查询，避免整个 KB 查询流程失效。
        """
        gaps_count = (
            len(completeness.get("critical_missing", []))
            + len(completeness.get("important_missing", []))
            + len(completeness.get("optimization_missing", []))  # v2.34: 优化需求专项缺口
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
                system_prompt=prompt_manager.get("clarify_agent", "KB_QUERY_GENERATOR_SYSTEM_PROMPT"),
                schema_hint={"queries": list},
                temperature=0.3,
                provider=kb_provider,
                agent_name="ClarifyAgent",
            )
            queries = result.get("queries", [])
            if queries:
                logger.info(
                    "[ClarifyAgent] LLM 生成 KB 查询: {} 条（上下文感知）",
                    len(queries),
                )
                # 构建合法 field 集合（防止 LLM 编造列表中不存在的缺口）
                valid_fields: set[str] = set()
                for m in completeness.get("critical_missing", []) + completeness.get("important_missing", []) + completeness.get("optimization_missing", []):
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
                # v2.31: 源头去重——按 field+question 双重去重，防止 LLM 返回重复查询
                # 同 field 不同 question 保留（不同维度的查询），相同 field+question 只保留一条
                seen_query_keys: set[str] = set()
                for q in queries:
                    if not isinstance(q, dict) or not q.get("question"):
                        continue
                    field = q.get("field", "")
                    if field and field not in valid_fields:
                        logger.info(
                            "[ClarifyAgent] 丢弃编造查询: field={} (不在缺口列表中)",
                            field,
                        )
                        continue
                    # 源头去重：field+question 前 40 字符
                    q_text = q["question"][:40]
                    query_dedup_key = f"{field}::{q_text}"
                    if query_dedup_key in seen_query_keys:
                        logger.info(
                            "[ClarifyAgent] 源头去重: 跳过重复 KB 查询 field={!r} question={!r}",
                            field, q["question"][:60],
                        )
                        continue
                    seen_query_keys.add(query_dedup_key)
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
            # v2.26: 不再降级——LLM 失败直接抛异常，由上层 _run_requirement_analysis
            # 捕获并标记任务 failed，前端展示"生成澄清问题失败"。
            # 旧版降级生成 "{desc}的相关规则和约束是什么？" 太模糊，用户无法分清是哪部分规则，
            # 且静默吞错后仍标记 completed，违反错误可见性原则。
            logger.error(
                "[ClarifyAgent] LLM KB 查询生成失败（不降级，中断流水线）: {}",
                str(e)[:300],
            )
            raise RuntimeError(f"生成知识库查询失败: {str(e)[:300]}") from e

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
        # v2.34: 兼容旧 KB 数据（business_rules 在顶层）和新数据（在 cross_fp_interactions.shared_business_rules）
        cfi_src = src_srr.get("cross_fp_interactions") or {}
        for k in keys:
            if k == "happy_path":
                v = flows.get("happy_path")
            elif k == "alternate_flows":
                v = flows.get("alternate_flows")
            elif k == "business_rules":
                # 优先取 shared_business_rules，回退到顶层 business_rules（旧数据）
                v = cfi_src.get("shared_business_rules") or src_srr.get(k)
            else:
                v = src_srr.get(k)
            if v:
                block[k] = v
        return block

    @staticmethod
    def _detect_baseline_focus(question: str) -> list[str]:
        """根据基线问题文本推断用户真正关心的 SRR 维度。

        修复「全量回答」：增量文档基线补全时，每个基线问题（如「正常流程」「业务规则」）
        只需返回对应维度，而非把整篇历史 SRR 块当作答案。本方法从问题文本识别出
        命中的维度 key，供 _query_kb_for_answers 精准提取与格式化。

        返回命中的维度 key 列表；未命中任何关键词时返回空列表（调用方回退到整块）。
        """
        q = (question or "").lower()
        hits: list[str] = []
        # 先判异常/边界（避免「异常流程」被「流程」误命中为正常流程）
        if any(k in q for k in ["异常", "边界", "分支", "alternate", "异常流程", "边界流程"]):
            hits.append("alternate_flows")
        # 正常流程 / 主流程 / 登录流程（不含裸「流程」，否则会误带异常流程）
        if any(k in q for k in ["正常流程", "主流程", "登录流程", "happy_path", "happy path"]):
            hits.append("happy_path")
        if any(k in q for k in ["数据约束", "数据字段", "字段约束", "字段", "约束", "data_constraint"]):
            hits.append("data_constraints")
        if any(k in q for k in ["业务规则", "业务", "规则", "business_rule"]):
            hits.append("business_rules")
        if any(k in q for k in ["前置", "前提", "precondition"]):
            hits.append("preconditions")
        if any(k in q for k in ["验收", "acceptance"]):
            hits.append("acceptance_criteria")
        return hits

    @staticmethod
    def _format_baseline_summary(block: dict, max_chars: int = 800) -> str:
        """将基线上下文块格式化为可读文本摘要，供 answer_text 展示。

        Args:
            block: _extract_baseline_block 返回的 dict，含 happy_path/alternate_flows/
                   data_constraints/business_rules/preconditions 等维度
            max_chars: 摘要最大字符数（防止过长）

        Returns:
            str: 可读的多行摘要文本
        """
        parts: list[str] = []

        hp = block.get("happy_path")
        if isinstance(hp, dict) and hp.get("steps"):
            steps = hp["steps"][:5]
            parts.append("【正常流程】")
            for s in steps:
                parts.append(f"  {s.get('order','')}. {s.get('action','')} → {s.get('expect','')}")

        af = block.get("alternate_flows")
        if isinstance(af, list) and af:
            parts.append("【异常/边界流程】")
            for a in af[:3]:
                if isinstance(a, dict):
                    parts.append(f"  · {a.get('trigger','')} → {a.get('expect','')}")
                else:
                    parts.append(f"  · {str(a)[:80]}")

        # v2.34: 兼容 KB 旧数据（顶层 business_rules）和新数据（cross_fp_interactions.shared_business_rules）
        cfi_block = block.get("cross_fp_interactions") or {}
        br = block.get("business_rules") or cfi_block.get("shared_business_rules") or []
        if isinstance(br, list) and br:
            parts.append("【业务规则】")
            for r in br[:5]:
                if isinstance(r, dict):
                    parts.append(f"  · {r.get('id','')} {r.get('rule', r.get('description',''))}")
                else:
                    parts.append(f"  · {str(r)[:80]}")

        dc = block.get("data_constraints")
        if isinstance(dc, list) and dc:
            parts.append("【数据约束】")
            for d in dc[:5]:
                if isinstance(d, dict):
                    parts.append(f"  · {d.get('field','')}: {d.get('constraints', d.get('constraint',''))}")
                else:
                    parts.append(f"  · {str(d)[:80]}")

        pc = block.get("preconditions")
        if isinstance(pc, list) and pc:
            parts.append("【前置条件】")
            for p in pc[:3]:
                parts.append(f"  · {p if isinstance(p, str) else str(p)[:80]}")

        ac = block.get("acceptance_criteria")
        if isinstance(ac, list) and ac:
            parts.append("【验收标准】")
            for c in ac[:8]:
                if isinstance(c, dict):
                    parts.append(f"  · {c.get('description', c.get('criterion', str(c)))}")
                else:
                    parts.append(f"  · {str(c)[:120]}")

        summary = "\n".join(parts)
        if len(summary) > max_chars:
            summary = summary[:max_chars] + "\n  ...(更多内容请展开来源详情)"
        return summary

    async def _query_kb_for_answers(
        self,
        srr: dict,
        questions: list[dict],
        all_similar_reqs: list[dict],
        pg_context: dict,
    ) -> list[dict]:
        """对每个问题查询知识库并尝试获取答案。

        v2.52: 不再接收外部 session，内部 DB 查询使用短生命周期 session。

        流程:
        1. 用问题文本搜索向量库 → 相似需求
        2. 用 PG 关联查询 → 模块依赖、测试用例、追溯矩阵
        3. 从搜索结果中提取答案
        4. 记录 Q&A 对

        Args:
            srr: 当前需求 SRR
            questions: _formulate_kb_questions 生成的问题列表
            all_similar_reqs: 全局搜索的相似需求（复用）
            pg_context: PG 增强上下文

        Returns:
            list[dict]: [{question, answer, answered, sources, field, priority, auto_filled}, ...]
        """
        answers = []

        for q in questions:
            # v2.18: 记录单条查询耗时（用于 KB 访问日志的 duration_ms）
            _t_q = time.time()

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
                    "duration_ms": int((time.time() - _t_q) * 1000),
                })
                continue

            # 搜索相关需求 — 使用 hybrid_searcher 混合检索（向量 + BM25 + RRF + 重排）
            # v2.19: 按 module 过滤，防止不同模块的相同功能点产生错误的知识库匹配
            # v2.52: 使用内部短生命周期 session
            # v2.53: 增加 doc_type="requirement" 过滤，避免 FP 向量混入补全上下文
            # （FP 向量的 srr_json 是单功能点 SRR，结构不同，混入会导致补全粒度错乱）
            try:
                from app.db.hybrid_search import hybrid_searcher
                from app.db.database import async_session as _as
                current_modules = srr.get("requirement", {}).get("modules", []) or []
                async with _as() as tmp_session:
                    matched = await hybrid_searcher.search_requirements(
                        query=q["question"], top_k=5,
                        product_id=srr.get("requirement", {}).get("product_id", ""),
                        modules=current_modules,
                        status="archived", session=tmp_session,
                        doc_type="requirement",
                    )
            except Exception as e:
                # 降级策略：KB 不可用 → 静默跳过 → 继续生成澄清问题 → 人工澄清兜底
                # 不影响生成流程，不注入诊断信息到 Prompt
                logger.warning("[ClarifyAgent] KB 查询失败: q={!r} error={}", q.get("question", "")[:80], str(e)[:200])
                matched = []

            if matched:
                sims = [round(m.get("similarity", 0), 3) for m in matched]
                logger.info("[ClarifyAgent] KB 命中: q={!r} count={} sims={}", q.get("question", "")[:60], len(matched), sims)

            # 按 go_live_date 降序排序：同一功能多个匹配时，上线日期靠后的优先
            matched.sort(
                key=lambda m: (m.get("metadata", {}) or {}).get("go_live_date", ""),
                reverse=True,
            )

            # P5 修复：按 req_id 去重，避免同一需求/FP 重复展示
            # 主需求向量以 req_id 标识，FP 向量以 FP-{fp_id} 标识。
            # 同一 req_id 可能因重复归档或多版本产生多个匹配，保留相似度最高的。
            # 注意：主需求与 FP 之间不互相去重（不同功能点有价值）。
            seen_req_ids: dict[str, dict] = {}
            for m in matched:
                meta = m.get("metadata", {}) or {}
                dedup_key = meta.get("req_id", m.get("id", ""))
                if not dedup_key:
                    continue
                sim = m.get("similarity", 0)
                if dedup_key not in seen_req_ids or sim > seen_req_ids[dedup_key].get("similarity", 0):
                    seen_req_ids[dedup_key] = m
            matched = list(seen_req_ids.values())

            # 从匹配需求中提取答案
            answer_text = ""
            sources = []
            for m in matched:
                # 问题→文档语义匹配阈值 0.40。
                # bge-large-zh 归一化向量下，具体问题与整篇 SRR 文本的余弦相似度
                # 天然偏低（典型 0.40-0.65），0.85/0.70 会误杀几乎所有有效结果。
                # 后续有 _verify_kb_answers 的 LLM 校验作为第二道质量关卡。
                if m.get("similarity", 0) < 0.40:
                    continue
                doc = m.get("document", "") or ""
                meta = m.get("metadata", {}) or {}
                req_id = meta.get("req_id", m.get("id", ""))
                srr_json_str = meta.get("srr_json", "")
                # 上线日期 + 项目版本（用于 KB 检索版本溯源）
                go_live = meta.get("go_live_date", "")
                proj_ver = meta.get("project_version", "")
                ver_tag = f"v{proj_ver}" if proj_ver else ""
                date_tag = f"上线: {go_live}" if go_live else ""
                meta_tags = ", ".join(t for t in [ver_tag, date_tag] if t)
                meta_suffix = f"（{meta_tags}, 相似度 {m.get('similarity',0):.2f}）" if meta_tags else f"（相似度 {m.get('similarity',0):.2f}）"

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
                    # 精准补全：只返回问题关心的维度，而非把整篇历史 SRR 块当作答案
                    focus_keys = self._detect_baseline_focus(q.get("question", ""))
                    if focus_keys:
                        focus_block: dict = {}
                        flows = src_srr.get("flows", {}) or {}
                        for k in focus_keys:
                            if k in ("happy_path", "alternate_flows"):
                                if flows.get(k):
                                    focus_block[k] = flows[k]
                            elif src_srr.get(k):
                                focus_block[k] = src_srr[k]
                        if not focus_block:
                            focus_block = baseline_block
                    else:
                        focus_block = baseline_block
                    summary = self._format_baseline_summary(focus_block)
                    answer_text += (
                        f"• {req_id}{meta_suffix}：\n"
                        f"{summary}\n"
                    )
                    sources.append({
                        "id": req_id,
                        "similarity": round(m.get("similarity", 0), 3),
                        "field": q["field"],
                        "value": focus_block,          # 精准维度（前端展示 / 来源详情）
                        "value_full": baseline_block,  # 完整历史 SRR 块（仅用于 _baseline_context 完整性）
                        # 整篇 SRR 文档（截断），供 _verify_kb_answers 的 LLM
                        # 从中提取「针对问题的具体片段」，而非返回整字段/整块
                        "document_full": json.dumps(src_srr, ensure_ascii=False)[:3000] if src_srr else "",
                        "go_live_date": go_live,
                        "project_version": proj_ver,
                    })
                elif field_val:
                    answer_text += f"• {req_id}{meta_suffix}：{str(field_val)[:300]}\n"
                    # ⚠️ 保留结构化原始值（不转字符串、不截断），供 _apply_kb_answers
                    # 写回 SRR 时维持字段类型；仅 answer_text 用于展示时截断。
                    sources.append({
                        "id": req_id,
                        "similarity": round(m.get("similarity", 0), 3),
                        "field": q["field"],
                        "value": field_val,
                        # 整篇 SRR 文档（截断），供 LLM 提取针对性片段
                        "document_full": json.dumps(src_srr, ensure_ascii=False)[:3000] if src_srr else "",
                        "go_live_date": go_live,
                        "project_version": proj_ver,
                    })

            answered = len(sources) > 0
            answers.append({
                "id": f"q_{q['field'].replace('.', '_')}",
                "field": q["field"],
                "missing_desc": q.get("missing_desc", ""),
                "context": q.get("missing_desc", ""),
                "priority": q.get("priority", "important"),
                "question": q["question"],
                # 透传基线补全标志，供 _apply_kb_answers 命中基线合并分支（修复 Bug 4）
                "_is_baseline": q.get("_is_baseline", False),
                "_gap_keys": q.get("_gap_keys", []),
                "answer": answer_text.strip() if answered else "知识库中未找到相关信息，需要人工补充",
                "answered": answered,
                "sources": sources,
                "auto_filled": False,
                "input_type": "text",
                "options": [],
                "default_answer": q.get("missing_desc", ""),
                "duration_ms": int((time.time() - _t_q) * 1000),  # v2.18: 单条查询耗时
            })

        # v2.11：LLM 校验检索结果质量（判断相关性、拒绝不相关答案、提取具体信息）
        answers = await self._verify_kb_answers(srr, answers)

        return answers

    async def _verify_kb_answers(
        self, srr: dict, answers: list[dict],
        kb_provider: str | None = None,
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
                # 优先用整篇 SRR 文档上下文（document_full），让 LLM 从中提取
                # 「针对问题的具体片段」；回退到字段值 value（机械整字段提取）
                doc_full = s.get("document_full")
                if doc_full:
                    docs.append(doc_full[:1500])
                else:
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
                system_prompt=prompt_manager.get("clarify_agent", "KB_ANSWER_VERIFIER_SYSTEM_PROMPT"),
                schema_hint={"verifications": list},
                temperature=0.1,
                provider=kb_provider,
                agent_name="ClarifyAgent",
            )
            verifications = result.get("verifications", []) or []
            for v in verifications:
                idx = v.get("index")
                if idx is None or idx >= len(answers):
                    continue
                if v.get("relevant"):
                    # 基线补全：answer 已是精准维度摘要，不再用整篇文档覆盖（修复「全量回答」）
                    if answers[idx].get("_is_baseline"):
                        pass
                    else:
                        # LLM 确认相关：用 LLM 提取的答案替换机械答案
                        extracted = v.get("extracted_answer", "")
                        if extracted:
                            answers[idx]["answer"] = extracted
                    # 保留 answered=True
                else:
                    # LLM 判定不相关：拒收
                    logger.info(
                        "[ClarifyAgent] KB 答案校验拒绝: query={} reason={}",
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
                "[ClarifyAgent] KB 答案校验 LLM 调用失败，保留原始答案: {}",
                str(e)[:200],
            )
            # LLM 失败时不改变答案（降级为原有机械答案，避免全丢弃）

        return answers

    async def _refine_unresolved_to_questions(
        self, questions: list[dict], srr: dict, kb_provider: str | None = None,
    ) -> list[dict]:
        """为 KB 未答转人工的澄清问题补充回答形式与候选选项（降低回答成本）。

        主流程第⑧步将 KB 未答问题直接转人工（input_type=text, options=[]），
        但用户期望可选项。本方法调用 LLM（UNRESOLVED_REFINER_SYSTEM_PROMPT）
        为每个问题判断 input_type 并生成 2-4 个候选选项；LLM 失败则降级保留原问题。

        Args:
            questions: 第⑧步构建的人工澄清问题列表（含 id/field/question/context/default_answer）
            srr: 当前需求的 SRR（用于提供需求上下文，辅助选项生成）
            kb_provider: KB 专属 LLM 供应商。若指定则优先使用，否则回退全局主 LLM。

        Returns:
            list[dict]: 补充了 input_type/options 的问题列表（原问题字段不变）
        """
        if not questions:
            return questions

        try:
            items = [
                {
                    "index": idx,  # v2.30: 用 index 作为唯一 key，避免 id 冲突
                    "id": q.get("id"),
                    "field": q.get("field"),
                    "question": q.get("question", ""),
                    "context": q.get("context", ""),
                    "default_answer": q.get("default_answer", ""),
                }
                for idx, q in enumerate(questions)
            ]
            req_summary = (srr.get("requirement", {}) or {}).get("summary", "")
            user_prompt = (
                f"【当期需求摘要】\n{req_summary}\n\n"
                f"【需补充回答形式与候选选项的澄清问题】\n"
                f"{json.dumps(items, ensure_ascii=False, indent=2)}\n\n"
                f"请为每个问题判断最合适的回答形式（text/select/multi_select），"
                f"若 select 则给出 2-4 个候选选项，并尽量给出建议默认答案。\n"
                f"⚠️ 必须为每个问题返回对应的精炼结果，使用相同的 index 标识。只输出 JSON。"
            )
            result = await self._llm.call_json(
                prompt=user_prompt,
                system_prompt=prompt_manager.get("clarify_agent", "UNRESOLVED_REFINER_SYSTEM_PROMPT"),
                schema_hint={"questions": list},
                temperature=0.2,
                provider=kb_provider,
                agent_name="ClarifyAgent",
            )
            # v2.30: 用 index 作为 key，避免相同 id 被覆盖
            refined_by_idx: dict[int, dict] = {}
            for r in result.get("questions", []) or []:
                idx_val = r.get("index")
                if isinstance(idx_val, int) and 0 <= idx_val < len(questions):
                    refined_by_idx[idx_val] = r
            for idx, q in enumerate(questions):
                r = refined_by_idx.get(idx)
                if not r:
                    # v2.54: LLM 未返回此问题的精炼结果，使用字段映射表补充默认选项
                    # 避免"建议答案缺失"——用户期望尽量有可选项降低回答成本
                    field = q.get("field", "")
                    if field:
                        default_q = _build_field_question(field, q.get("description", ""), srr)
                        if default_q.get("options"):
                            q["input_type"] = default_q["input_type"]
                            q["options"] = default_q["options"]
                            if not q.get("default_answer"):
                                q["default_answer"] = default_q.get("default_answer", "")
                    continue
                it = r.get("input_type")
                if it in ("text", "select", "multi_select"):
                    q["input_type"] = it
                opts = r.get("options") or []
                if isinstance(opts, list):
                    q["options"] = [str(o) for o in opts][:6]
                # v2.54: 若 LLM 返回 select/multi_select 但 options 为空，用模板补充
                if q.get("input_type") in ("select", "multi_select") and not q.get("options"):
                    field = q.get("field", "")
                    if field:
                        default_q = _build_field_question(field, q.get("description", ""), srr)
                        if default_q.get("options"):
                            q["options"] = default_q["options"]
                if r.get("question"):
                    q["question"] = r["question"]
                if r.get("default_answer"):
                    q["default_answer"] = r["default_answer"]
            logger.info("[ClarifyAgent] 未答问题选项精炼完成: {} 题", len(questions))
        except Exception as e:
            # v2.26: 不再降级——LLM 精炼失败直接抛异常，由上层中断流水线。
            # 旧版降级保留 text + 空选项，导致用户看到模糊问题无法分清是哪部分规则，
            # 违反错误可见性原则。
            logger.error(
                "[ClarifyAgent] 未答问题选项精炼失败（不降级，中断流水线）: {}",
                str(e)[:300],
            )
            raise RuntimeError(f"生成澄清问题选项失败: {str(e)[:300]}") from e

        return questions

    async def _resolve_field_conflict(
        self,
        existing_value: Any,
        existing_source: str,
        kb_value: Any,
        kb_similarity: float,
        field: str,
        kb_source_req_id: str = "",
    ) -> tuple[Any, str, dict | None]:
        """字段冲突解决：返回 (final_value, final_source, conflict_record)。

        对应 docs/01-requirement-parsing.md §4.4.1 决策矩阵。
        v2.41（乙任务）：用户输入/人工已答值与 KB 值冲突时，先尝试 LLM 裁决
        （判断同义/KB更完整/用户优先），仅 LLM 无法裁决时才转人工。

        Args:
            existing_value: 字段当前值
            existing_source: 字段当前来源标记（USER_INPUT/HUMAN_ANSWERED/KB_INFERRED/...）
            kb_value: KB 补全建议值
            kb_similarity: KB 检索相似度
            field: 字段路径（用于冲突记录）
            kb_source_req_id: KB 来源需求 ID

        Returns:
            tuple: (最终值, 最终来源标记, 冲突记录或None)
        """
        # 字段为空 → 直接填充
        if existing_value is None or existing_value == "" or existing_value == []:
            return kb_value, "KB_INFERRED", None

        # 值相同 → 合并来源，提升置信
        if existing_value == kb_value:
            merged_source = f"{existing_source}+KB_CORROBORATED"
            return existing_value, merged_source, None

        # 值不同 → 按优先级处理
        existing_priority = FIELD_PRIORITY.get(existing_source, 0)
        if existing_priority <= FIELD_PRIORITY["HUMAN_ANSWERED"]:
            # 用户输入或人工回答 → 先尝试 LLM 裁决（乙任务），无法裁决才转人工
            llm_resolution = await self._llm_resolve_conflict(
                existing_value, kb_value, field, kb_similarity,
            )
            if llm_resolution is not None:
                final_value, final_source, resolution_label, reason = llm_resolution
                # 记录冲突但标记已 LLM 解决（可追溯）
                conflict = {
                    "field": field,
                    "existing_value": existing_value,
                    "existing_source": existing_source,
                    "suggested_value": kb_value,
                    "suggested_source": "KB_INFERRED",
                    "kb_similarity": kb_similarity,
                    "kb_source_req_id": kb_source_req_id,
                    "resolution": f"llm_{resolution_label}",
                    "llm_reason": reason,
                    "final_value": final_value,
                }
                return final_value, final_source, conflict
            # LLM 无法裁决 → 转 pending_human
            conflict = {
                "field": field,
                "existing_value": existing_value,
                "existing_source": existing_source,
                "suggested_value": kb_value,
                "suggested_source": "KB_INFERRED",
                "kb_similarity": kb_similarity,
                "kb_source_req_id": kb_source_req_id,
                "resolution": "pending_human",
            }
            return existing_value, existing_source, conflict
        elif existing_source == "KB_INFERRED":
            # 已有 KB 值 vs 新 KB 值 → 相似度更高的胜出，避免抖动
            return kb_value, "KB_INFERRED", None
        else:
            # PATTERN_DEFAULT / LLM_INFERRED → KB 优先
            return kb_value, "KB_INFERRED", None

    async def _llm_resolve_conflict(
        self,
        existing_value: Any,
        kb_value: Any,
        field: str,
        kb_similarity: float,
    ) -> tuple[Any, str, str, str] | None:
        """LLM 裁决字段冲突（乙任务）。

        调用 LLM 判断两个候选值是否同义/KB更完整/用户优先/需转人工。
        仅对"语义可判定"的冲突裁决，无法判定或调用失败时返回 None（回退到 pending_human）。

        Returns:
            (final_value, final_source, resolution_label, reason) 或 None
            - resolution_label: same_meaning / kb_more_complete / user_authoritative
            - final_source: KB_INFERRED（同义/KB更完整）或 原 existing_source（用户优先）
        """
        # 防御：值过长或非字符串/数值类型不裁决（避免 LLM 误判复杂结构）
        try:
            ev_str = str(existing_value)
            kv_str = str(kb_value)
        except Exception:
            return None
        if len(ev_str) > 500 or len(kv_str) > 500:
            return None  # 超长值不裁决

        user_prompt = (
            f"字段路径: {field}\n"
            f"用户输入值: {ev_str}\n"
            f"知识库推断值: {kv_str}\n"
            f"KB相似度: {kb_similarity:.2f}\n\n"
            "请判断这两个值是否可以自动裁决。"
        )
        try:
            result = await self._llm.call_json(
                prompt=user_prompt,
                system_prompt=prompt_manager.get("clarify_agent", "FIELD_CONFLICT_RESOLVER_SYSTEM_PROMPT"),
                schema_hint={"resolution": str, "final_value": str, "reason": str},
                temperature=0.1,
                agent_name="ClarifyAgent",
            )
        except Exception as e:
            logger.warning("[ClarifyAgent] 冲突裁决 LLM 调用失败(回退人工): field={}, error={}", field, str(e)[:150])
            return None

        if not isinstance(result, dict):
            return None
        resolution = result.get("resolution", "")
        reason = str(result.get("reason", ""))[:100]
        final_value_raw = result.get("final_value")

        # LLM 判定需转人工 → 返回 None 让上层走 pending_human
        if resolution == "pending_human" or final_value_raw is None:
            return None

        # 保留原有值类型：若原值是字符串，用 LLM 返回的字符串；否则尝试保持原类型
        if resolution == "user_authoritative":
            return existing_value, "USER_INPUT", "user_authoritative", reason
        elif resolution in ("same_meaning", "kb_more_complete"):
            # final_value 优先用 LLM 建议，但若类型不匹配则用 KB 值
            if isinstance(existing_value, str) and isinstance(final_value_raw, str):
                return final_value_raw, "KB_INFERRED", resolution, reason
            else:
                return kb_value, "KB_INFERRED", resolution, reason
        else:
            return None  # 未知 resolution → 回退人工

    async def _apply_kb_answers(
        self, srr: dict, kb_answers: list[dict]
    ) -> tuple[dict, list[dict], list[dict]]:
        """将 KB 答案应用到 SRR 中（含字段冲突处理）。

        v2.12: 新增字段冲突优先级处理（docs/01 §4.4.1）。
        - 字段为空 → 直接填充，标记 KB_INFERRED
        - 字段已有值且来源优先级高（USER_INPUT/HUMAN_ANSWERED）→ 不覆盖，记录冲突
        - 字段已有值且来源优先级低（PATTERN_DEFAULT/LLM_INFERRED）→ KB 覆盖

        Args:
            srr: 当前 SRR（已拷贝）
            kb_answers: _query_kb_for_answers 返回的答案列表

        Returns:
            tuple[dict, list[dict], list[dict]]:
                (更新后的 SRR, 补全记录列表, 字段冲突列表)
        """
        augmentations: list[dict] = []
        field_conflicts: list[dict] = []
        # 字段来源标记（临时字段，不写入正式 SRR Schema）
        field_sources: dict[str, str] = srr.setdefault("_field_sources", {})

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
            # 不覆盖文档自身已描述的内容（存入 _baseline_context 字段）。
            # v2.40: 移除 additional_details，baseline 仅写入 _baseline_context
            if a.get("_is_baseline") and isinstance(value, dict) and value:
                existing = srr.get("_baseline_context")
                existing = existing if isinstance(existing, dict) else {}
                # 用完整历史块（value_full）保证 _baseline_context 维度完整；
                # value 本身是精准维度，仅用于来源展示，不丢失其他维度。
                full_block = best.get("value_full") or value
                merged = dict(full_block)
                for k, v in existing.items():
                    merged.setdefault(k, v)
                srr["_baseline_context"] = merged
                a["auto_filled"] = True
                augmentations.append({
                    "field": field,
                    "source_req_id": best["id"],
                    "similarity": best["similarity"],
                    "value": full_block,
                    "_is_baseline": True,
                })
                continue

            # 字段冲突优先级处理（docs/01 §4.4.1）
            existing_value = self._get_nested_value(srr, field)
            # v2.18: 区分"有值但无来源标记"和"用户明确输入"
            # 无来源标记 → 视为 LLM_INFERRED（可被 KB 覆盖），避免 KB 补全失效
            existing_source = field_sources.get(field)
            if existing_source is None:
                existing_source = "LLM_INFERRED" if existing_value else ""
            final_value, final_source, conflict = await self._resolve_field_conflict(
                existing_value, existing_source, value, best.get("similarity", 0),
                field, best.get("id", ""),
            )

            if conflict:
                # 有冲突 → 记录冲突（pending_human 或 llm_resolved 均记录可追溯）
                field_conflicts.append(conflict)
                resolution_tag = conflict.get("resolution", "pending_human")
                logger.info("[ClarifyAgent] 字段冲突: field={}, existing={}({}) vs kb={}({:.2f}), resolution={}",
                            field, str(existing_value)[:50], existing_source,
                            str(value)[:50], best.get("similarity", 0), resolution_tag)
                # v2.53: LLM 裁决成功的冲突（llm_*）使用裁决值写入 SRR，不转人工
                # 仅 pending_human 才 continue（必须人工确认）
                if not resolution_tag.startswith("llm_"):
                    continue
                # llm_* → fall through，使用 final_value/final_source 写入 SRR

            try:
                self._set_nested_value(srr, field, final_value)
                field_sources[field] = final_source
                a["auto_filled"] = True
                augmentations.append({
                    "field": field,
                    "source_req_id": best["id"],
                    "similarity": best["similarity"],
                    "value": final_value,
                })
            except Exception:
                pass

        return srr, augmentations, field_conflicts

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
        # v2.34: 合并 optimization_missing（优化需求专项字段也尝试 KB 补全）
        missing_fields = (
            completeness.get("critical_missing", [])
            + completeness.get("important_missing", [])
            + completeness.get("optimization_missing", [])
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
    # PG 关系型数据增强（v2.6）
    # ========================================================================

    async def _enrich_with_pg(
        self, session, srr: dict, similar_reqs: list[dict]
    ) -> dict:
        """从 PG 关系型数据补充上下文，增强 KB 补全效果。

        利用产品/项目/模块/标签/依赖图谱/追溯矩阵等关系信息，
        为 KB 自动补全和 LLM 问题生成提供更丰富的上下文。

        Args:
            session: SQLAlchemy AsyncSession
            srr: 当前需求的 SRR
            similar_reqs: 向量库检索到的相似需求列表

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

        # 1. 查询当前需求的 PG 记录，获取 project_id 和 tags
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
        pg_context: dict | None = None,
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
            pg_context: PG 关系型增强上下文（v2.6）

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
                agent_name="ClarifyAgent",
            )
            # 后处理：仅过滤掉 optional 问题（保留全部 critical/important，不截断）
            questions = result.get("questions", [])
            questions = [q for q in questions if q.get("priority") != "optional"]
            result["questions"] = questions
            return result
        except Exception as e:
            logger.error(
                "[ClarifyAgent] LLM 问题生成失败: {}",
                str(e)[:300],
            )
            raise

    # ========================================================================
    # 优化需求场景检测（v2.30 新增）
    # ========================================================================

    # 优化类需求关键词——命中任一即视为优化需求
    # v2.53: 拆分强/弱优化关键词，避免 UI 描述中的"调整/完善/增强"误判
    # - STRONG: 明确指向"对已有系统的修改"（重构/迁移/改造/改版/重做/替换/提速）
    #   无论出现在标题/摘要/正文，都视为优化需求信号
    # - WEAK: 语义模糊（调整/完善/增强/改进/升级/迭代/优化），常见于新功能细节描述
    #   仅当出现在标题/摘要时才算优化需求；正文命中不算（如"图标排版调整为水平居中"）
    OPTIMIZATION_KEYWORDS_STRONG: tuple[str, ...] = (
        "重构", "迁移", "改造", "改版", "重做", "替换", "提速",
        "refactor",
    )
    OPTIMIZATION_KEYWORDS_WEAK: tuple[str, ...] = (
        "优化", "改进", "升级", "调整", "增强", "完善", "迭代",
        "optimize", "improve", "upgrade", "enhance",
    )
    # 兼容旧引用（部分代码可能仍引用 OPTIMIZATION_KEYWORDS）
    OPTIMIZATION_KEYWORDS: tuple[str, ...] = (
        OPTIMIZATION_KEYWORDS_STRONG + OPTIMIZATION_KEYWORDS_WEAK
    )

    async def _llm_detect_optimization(self, srr: dict, kb_provider: str | None = None) -> None:
        """用 LLM 语义判断需求是否为优化/改进已有系统类需求。

        v2.54: 替代关键词匹配。LLM 能理解需求本质（"改已有的" vs "建全新的"），
        避免弱关键词（完善/增强/调整）在新功能描述中误判为优化需求。

        判断结果写入 srr["_llm_is_optimization"] = {
            "is_optimization": bool,
            "reason": str,
            "confidence": str
        }
        供 _check_completeness 和 _detect_optimization_impact 同步读取。

        LLM 调用失败时不写入 _llm_is_optimization（下游降级到强词匹配）。

        Args:
            srr: SRR JSON 字典（会被原地修改）
            kb_provider: KB 专属 LLM 供应商
        """
        # 若已有 LLM 判断结果（如重新生成时上一轮已判断），直接复用
        if isinstance(srr.get("_llm_is_optimization"), dict):
            return

        # 用户已明确"不影响历史数据" -> 无需再判断
        ldi = srr.get("legacy_data_impact") or {}
        if isinstance(ldi, dict):
            impact_scope = str(ldi.get("impact_scope", "") or "")
            user_clarification = str(ldi.get("user_clarification", "") or "")
            no_impact_markers = ("不影响历史数据", "仅新增功能", "无影响")
            if any(m in impact_scope or m in user_clarification for m in no_impact_markers):
                srr["_llm_is_optimization"] = {"is_optimization": False, "reason": "用户已确认不影响历史数据", "confidence": "high"}
                return

        req = srr.get("requirement", {}) or {}
        title = req.get("title", "") or ""
        summary = req.get("summary", "") or ""
        raw_text = (srr.get("_raw_text", "") or "")[:3000]

        user_prompt = (
            f"【需求标题】\n{title}\n\n"
            f"【需求摘要】\n{summary}\n\n"
            f"【需求原文（前3000字）】\n{raw_text}\n\n"
            f"请判断这个需求是「新建功能」还是「优化/改进已有系统」。"
        )

        try:
            result = await self._llm.call_json(
                prompt=user_prompt,
                system_prompt=prompt_manager.get("clarify_agent", "OPTIMIZATION_DETECTOR_SYSTEM_PROMPT"),
                schema_hint={"is_optimization": bool, "reason": str, "confidence": str},
                temperature=0.1,
                provider=kb_provider,
                agent_name="ClarifyAgent",
            )
            is_opt = bool(result.get("is_optimization", False))
            reason = str(result.get("reason", ""))[:100]
            confidence = str(result.get("confidence", "medium"))[:10]
            srr["_llm_is_optimization"] = {
                "is_optimization": is_opt,
                "reason": reason,
                "confidence": confidence,
            }
            logger.info(
                "[ClarifyAgent] LLM 优化需求判断: is_optimization={}, confidence={}, reason={}",
                is_opt, confidence, reason[:50],
            )
        except Exception as e:
            # LLM 失败时不写入 _llm_is_optimization，下游降级到强词匹配
            logger.warning(
                "[ClarifyAgent] LLM 优化需求判断失败，降级到强词匹配: {}",
                str(e)[:200],
            )

    def _detect_optimization_impact(self, srr: dict) -> list[dict]:
        """检测需求是否为优化/改进类，若是则返回「历史数据影响」问题列表。

        v2.54: 改用 LLM 语义判断（结果存于 srr["_llm_is_optimization"]），
        替代关键词匹配。LLM 能理解需求本质（"改已有的"vs"建全新的"），
        避免弱关键词（完善/增强/调整）在新功能描述中误判。
        LLM 不可用时降级到强关键词匹配（仅重构/迁移/改造等强词）。

        v2.32: 扩展为返回两个问题：
        1. multi_select 问题：判断是否影响历史数据及影响类型
        2. text 输入问题：让用户填写具体影响的历史数据范围（受影响的表/字段/数据量等）
           该问题的答案将写入 SRR.legacy_data_impact.user_clarification

        Args:
            srr: SRR JSON 字典

        Returns:
            list[dict]: 若为优化需求，返回澄清问题列表（含 multi_select + text 两个问题）；
                        否则返回空列表
        """
        req = srr.get("requirement", {}) or {}

        # v2.53: 尊重用户已答结果--若上一轮澄清用户已选择"不影响历史数据"，
        # 不再重复生成历史数据影响问题（避免重新生成后死循环提问）
        ldi = srr.get("legacy_data_impact") or {}
        if isinstance(ldi, dict):
            impact_scope = str(ldi.get("impact_scope", "") or "")
            user_clarification = str(ldi.get("user_clarification", "") or "")
            # 用户已明确"不影响历史数据" -> 跳过优化需求检测
            no_impact_markers = ("不影响历史数据", "仅新增功能", "无影响")
            if any(m in impact_scope or m in user_clarification for m in no_impact_markers):
                logger.info(
                    "[ClarifyAgent] 用户已确认不影响历史数据，跳过优化需求检测: impact_scope={!r}",
                    impact_scope[:80],
                )
                return []

        # v2.54: 优先使用 LLM 语义判断结果
        llm_opt = srr.get("_llm_is_optimization")
        is_optimization = False
        detect_source = ""
        if isinstance(llm_opt, dict):
            is_optimization = bool(llm_opt.get("is_optimization", False))
            detect_source = f"LLM({llm_opt.get('reason', '')[:30]})"
        else:
            # 降级：LLM 未返回结果时，仅用强词匹配（重构/迁移/改造等）
            title = (req.get("title", "") or "").lower()
            summary = (req.get("summary", "") or "").lower()
            raw_text = (srr.get("_raw_text", "") or "")[:5000].lower()
            title_summary = f"{title}\n{summary}"
            strong_hits = [kw for kw in self.OPTIMIZATION_KEYWORDS_STRONG if kw in title_summary or kw in raw_text]
            is_optimization = bool(strong_hits)
            detect_source = f"强词兜底({','.join(strong_hits[:3])})" if strong_hits else "无命中"

        if not is_optimization:
            logger.info("[ClarifyAgent] 优化需求检测: {} -> 非优化需求", detect_source)
            return []

        logger.info("[ClarifyAgent] 优化需求检测: {} -> 是优化需求，生成历史数据影响问题", detect_source)

        # 生成历史数据影响问题
        req_title = req.get("title", "当前需求") or "当前需求"
        keyword_hint = detect_source

        # 问题1：multi_select——判断是否影响历史数据及影响类型
        impact_type_question = {
            "id": "kb_optimization_impact",
            "field": "optimization_impact",
            "priority": "critical",
            "category": "impact_analysis",
            "question": f"【{req_title}】检测到这是优化/改进类需求，请问此次优化是否会影响已有历史数据？",
            "context": (
                f"需求文档中检测到优化关键词（{keyword_hint}）。"
                "优化类需求可能涉及数据结构变更、接口变更、业务逻辑调整等，"
                "可能影响已有历史数据的可用性、一致性或完整性。"
                "请明确影响范围，以便生成对应的回归测试和数据迁移测试用例。"
            ),
            "default_answer": "",
            "input_type": "multi_select",
            "options": [
                "不影响历史数据，仅新增功能",
                "影响历史数据结构（需数据迁移）",
                "影响历史数据内容（需数据清洗）",
                "影响接口兼容性（需向后兼容）",
                "影响业务逻辑（需回归测试）",
                "不确定，需要进一步评估",
            ],
        }

        # 问题2：text 输入——让用户填写具体影响的历史数据范围
        # 答案将写入 SRR.legacy_data_impact.user_clarification，供 LLM 生成完整影响分析
        impact_scope_question = {
            "id": "kb_optimization_impact_scope",
            "field": "legacy_data_impact.user_clarification",
            "priority": "important",
            "category": "impact_analysis",
            "question": f"请描述此次优化具体影响的历史数据范围（可留空）",
            "context": (
                "请列出受影响的数据实体/数据库表/字段、预估数据量、上下游系统等。"
                "例如：「影响 user 表的 phone 字段（约 50 万行），需从 varchar(11) 扩展为 varchar(20)；"
                "订单表 order 的 status 枚举值需新增 2 个状态（约 1000 万行）。」"
                "该描述将作为历史数据影响分析的输入，用于生成数据迁移与回归测试用例。"
                "若上一题已选择「不影响历史数据」，本题可留空。"
            ),
            "default_answer": "",
            "input_type": "text",
            "options": [],
        }

        return [impact_type_question, impact_scope_question]

    # ========================================================================
    # 逻辑矛盾检测（v2.39 新增）
    # ========================================================================
    # 识别需求文档中明显的自相矛盾表述（如"已存在仓房信息且不存在仓房信息"）。
    # 这类矛盾通常源于：
    #   1. 同一句中出现"X"与"不X/无X"的对立表述（存在/不存在、必须/不能、是/不是）
    #   2. 错别字导致的逻辑冲突（如"仓房"误写为"仓库"或反向）
    # 规则层只能做明显的模式匹配守卫，复杂语义矛盾由 LLM 在系统提示强化下识别。

    # 逻辑矛盾正则模式：在同一短句（30 字符内）出现对立表述
    LOGICAL_CONTRADICTION_PATTERNS: tuple[tuple[str, str], ...] = (
        # 存在 X 且 不/无 存在 X（"已存在仓房信息且不存在仓房信息"）
        (
            r'(存在|有)([^，。；,;]{1,20}?)\s*(?:且|又|并|而|但|同时)\s*(?:不|无|未|没有)(?:存在|有)\2',
            '同一句中「存在{X}」与「不存在{X}」自相矛盾'
        ),
        # 必须 X 且 不能/不可/不得 X
        (
            r'(?:必须|应当|需要|应)([^，。；,;]{1,20}?)\s*(?:且|又|并|而|但|同时)\s*(?:不能|不可|不得|禁止|不应)(?:\1)',
            '同一句中「必须{X}」与「不能{X}」自相矛盾'
        ),
        # 是 X 且 不是 X
        (
            r'是([^，。；,;]{1,20}?)\s*(?:且|又|并|而|但|同时)\s*不是\1',
            '同一句中「是{X}」与「不是{X}」自相矛盾'
        ),
        # 允许 X 且 禁止 X
        (
            r'(?:允许|可以|支持)([^，。；,;]{1,20}?)\s*(?:且|又|并|而|但|同时)\s*(?:禁止|不允许|不可|不能)(?:\1)',
            '同一句中「允许{X}」与「禁止{X}」自相矛盾'
        ),
        # 包含 X 且 不包含 X
        (
            r'(?:包含|包括|含有)([^，。；,;]{1,20}?)\s*(?:且|又|并|而|但|同时)\s*(?:不包含|不包括|不含)(?:\1)',
            '同一句中「包含{X}」与「不包含{X}」自相矛盾'
        ),
    )

    # 易混淆术语对（错别字检测）：同一句中出现则可能存在错别字导致的逻辑冲突
    CONFUSABLE_TERM_PAIRS: tuple[tuple[str, str], ...] = (
        ('仓房', '仓库'),  # 仓储系统常见
        ('账单', '订单'),
        ('用户', '客户'),
        ('商品', '产品'),
        ('上架', '下架'),
        ('启用', '禁用'),
        ('登录', '登出'),
        ('新增', '删除'),
        ('入库', '出库'),
    )

    def _detect_logical_contradictions(self, srr: dict) -> list[dict]:
        """检测需求文档中明显的逻辑矛盾与错别字冲突。

        v2.39: 规则层守卫——识别「已存在 X 且不存在 X」等明显自相矛盾表述，
        以及错别字导致的逻辑冲突（如「仓房」与「仓库」混用）。
        检测到的问题作为 critical 级别的 conflict 类别问题加入澄清列表。

        扫描范围：
        1. 原始需求文本（_raw_text，最权威）
        2. SRR 中各字符串字段（summary、业务规则、流程步骤等）

        Args:
            srr: SRR JSON 字典

        Returns:
            list[dict]: 逻辑矛盾问题列表，每项含 category="conflict"、priority="critical"
        """
        questions: list[dict] = []
        req = srr.get("requirement", {}) or {}
        req_title = req.get("title", "当前需求") or "当前需求"

        # 收集待扫描文本（带来源标识，便于生成精准问题）
        texts_to_scan: list[tuple[str, str]] = []  # (text, source_label)

        # 1. 原始需求文本
        raw_text = srr.get("_raw_text", "") or ""
        if raw_text:
            texts_to_scan.append((raw_text, "原始需求文档"))

        # 2. SRR 各字段文本
        summary = req.get("summary", "") or ""
        if summary:
            texts_to_scan.append((summary, "功能概述"))

        # flows 中的步骤描述
        flows = srr.get("flows", {}) or {}
        hp = flows.get("happy_path", {}) or {}
        for step in (hp.get("steps") or [])[:10]:
            if isinstance(step, dict):
                action = step.get("action", "") or ""
                expect = step.get("expect", "") or ""
                if action:
                    texts_to_scan.append((action, "正常流程步骤"))
                if expect:
                    texts_to_scan.append((expect, "正常流程预期"))

        for af in (flows.get("alternate_flows") or [])[:5]:
            if isinstance(af, dict):
                trigger = af.get("trigger", "") or ""
                expect = af.get("expect", "") or ""
                if trigger:
                    texts_to_scan.append((trigger, "异常流程触发条件"))
                if expect:
                    texts_to_scan.append((expect, "异常流程预期"))

        # 业务规则
        cfi = srr.get("cross_fp_interactions") or {}
        br = cfi.get("shared_business_rules") or srr.get("business_rules") or []
        if isinstance(br, list):
            for r in br[:8]:
                if isinstance(r, dict):
                    rule_text = r.get("rule", "") or r.get("description", "") or ""
                    if rule_text:
                        texts_to_scan.append((rule_text, "业务规则"))
                elif isinstance(r, str) and r:
                    texts_to_scan.append((r, "业务规则"))

        # 验收标准
        ac = srr.get("acceptance_criteria") or []
        if isinstance(ac, list):
            for a in ac[:5]:
                if isinstance(a, dict):
                    desc = a.get("description", "") or ""
                    if desc:
                        texts_to_scan.append((desc, "验收标准"))
                elif isinstance(a, str) and a:
                    texts_to_scan.append((a, "验收标准"))

        # 全局约束
        gc = cfi.get("global_constraints") or []
        if isinstance(gc, list):
            for g in gc[:5]:
                if isinstance(g, str) and g:
                    texts_to_scan.append((g, "全局约束"))

        # 按句拆分扫描（避免跨句误匹配）
        sentence_split = re.compile(r'[，。；,;\n]')
        seen_contradictions: set[str] = set()  # 去重

        for text, source in texts_to_scan:
            if not text or len(text) < 4:
                continue
            # 拆分为短句扫描（避免跨句误匹配，且短句内的矛盾更精准）
            # 不再扫描整段原文——跨句匹配会产生与短句匹配相同的矛盾，导致重复
            sentences = [s.strip() for s in sentence_split.split(text) if s.strip()]
            # 若文本本身就是单句（无分隔符），则扫描原文
            paragraphs = sentences if sentences else [text]

            for paragraph in paragraphs:
                if len(paragraph) < 4:
                    continue

                # 模式1：直接对立表述（存在 X 且 不存在 X）
                for pattern, desc_template in self.LOGICAL_CONTRADICTION_PATTERNS:
                    matches = re.finditer(pattern, paragraph)
                    for m in matches:
                        matched_text = m.group(0)
                        # 提取被对立的实体（group 1 通常是 X）
                        entity = m.group(1) if m.lastindex and m.lastindex >= 1 else ""
                        # 去重 key（避免同一段落多次匹配同一矛盾）
                        dedup_key = f"{source}::{matched_text}"
                        if dedup_key in seen_contradictions:
                            continue
                        seen_contradictions.add(dedup_key)

                        desc = desc_template.replace('{X}', f'「{entity}」') if entity else desc_template
                        questions.append({
                            "id": f"contradiction_{len(questions) + 1}",
                            "field": "requirement.logic_consistency",
                            "priority": "critical",
                            "category": "conflict",
                            "question": (
                                f"【{req_title}】{source}中检测到逻辑矛盾："
                                f"「{matched_text}」——{desc}。"
                                f"请确认该表述的真实意图，并修正矛盾内容。"
                            ),
                            "context": (
                                f"该矛盾位于{source}中。逻辑矛盾会导致测试预期不确定——"
                                f"无法判断应验证「{entity}」为存在还是不存在。"
                                f"常见原因：①笔误（如「仓房」误写为「仓库」）；"
                                f"②复制粘贴遗留；③需求变更未同步更新。"
                                f"请明确正确表述后再继续生成 SRR。"
                            ),
                            "default_answer": "",
                            "input_type": "select",
                            "options": [
                                f"应保留「存在{entity}」，删除矛盾部分",
                                f"应保留「不存在{entity}」，删除矛盾部分",
                                "为错别字，请修正后重新表述",
                                "其他情况（请在下方补充说明）",
                            ],
                        })
                        logger.info(
                            "[ClarifyAgent] 逻辑矛盾检测命中: source={} matched={!r} desc={}",
                            source, matched_text[:60], desc,
                        )

                # 模式2：易混淆术语对（错别字检测）
                # 同一段落中出现易混淆术语对，且段落涉及"存在/不存在/必须/不能"等对立词
                for term_a, term_b in self.CONFUSABLE_TERM_PAIRS:
                    if term_a in paragraph and term_b in paragraph:
                        # 进一步检查是否涉及对立表述
                        has_opposition = any(
                            opp in paragraph for opp in
                            ['存在', '不存在', '必须', '不能', '禁止', '允许', '是', '不是']
                        )
                        if not has_opposition:
                            continue
                        # 避免误报：术语对距离过远（>30字符）可能是合理并列
                        idx_a = paragraph.find(term_a)
                        idx_b = paragraph.find(term_b)
                        distance = abs(idx_a - idx_b)
                        if distance > 30:
                            continue

                        matched_text = paragraph[max(0, min(idx_a, idx_b) - 5):
                                                 max(idx_a, idx_b) + max(len(term_a), len(term_b)) + 5]
                        dedup_key = f"{source}::{term_a}/{term_b}::{matched_text}"
                        if dedup_key in seen_contradictions:
                            continue
                        seen_contradictions.add(dedup_key)

                        questions.append({
                            "id": f"contradiction_{len(questions) + 1}",
                            "field": "requirement.logic_consistency",
                            "priority": "critical",
                            "category": "conflict",
                            "question": (
                                f"【{req_title}】{source}中检测到可能的错别字冲突："
                                f"同一句中同时出现「{term_a}」与「{term_b}」，"
                                f"且涉及对立表述（存在/不存在、必须/不能等）。"
                                f"原文片段：「{matched_text}」。请确认是否为错别字。"
                            ),
                            "context": (
                                f"该问题位于{source}中。"
                                f"「{term_a}」与「{term_b}」是常见易混淆术语，"
                                f"在同一句中并列出现且伴随对立表述时，"
                                f"通常意味着其中一个是错别字，需要人工确认正确表述。"
                                f"错别字会直接导致测试预期错误，必须澄清。"
                            ),
                            "default_answer": "",
                            "input_type": "select",
                            "options": [
                                f"应统一为「{term_a}」",
                                f"应统一为「{term_b}」",
                                f"两者是不同概念，无需修改（请在下方补充说明各自含义）",
                                "其他情况（请在下方补充说明）",
                            ],
                        })
                        logger.info(
                            "[ClarifyAgent] 错别字冲突检测命中: source={} terms={}/{} distance={}",
                            source, term_a, term_b, distance,
                        )

        if questions:
            logger.info(
                "[ClarifyAgent] 逻辑矛盾检测: 共识别 {} 个矛盾点",
                len(questions),
            )
        return questions

    def _ensure_critical_questions(
        self,
        llm_questions: list[dict],
        unresolved_qa: list[dict],
        completeness: dict,
        srr: dict | None = None,
    ) -> list[dict]:
        """保证 critical 字段缺失一定被问（规则层强制守卫）。

        方向A（v2.9）：LLM 主导问题生成，但 critical 字段（summary /
        acceptance_criteria）缺失会直接阻断 SRR 生成，必须兜底确保被问到，
        避免 LLM 漏判导致需求缺口漏网。important / 语义遗漏 / 歧义完全由 LLM 决定。

        v2.31: 统一追加问题的结构——使用 _build_field_question 重新构建问题，
        避免追加原始 qa 字典（id 格式不同、缺少 category 等字段）导致结构不一致。
        同时按 field+question 双重去重，避免追加与已有问题重复的 qa。
        """
        critical_fields = {m["field"] for m in completeness.get("critical_missing", [])}
        # 收集已有问题的 field 和 question 前40字符，用于去重
        llm_fields = {q.get("field") for q in llm_questions}
        existing_q_keys: set[str] = set()
        for q in llm_questions:
            f = q.get("field", "")
            qt = (q.get("question", "") or "")[:40]
            existing_q_keys.add(f"{f}::{qt}")

        guarded = list(llm_questions)
        for qa in unresolved_qa:
            field = qa.get("field", "")
            if field in critical_fields and field not in llm_fields:
                # v2.31: 双重去重——field+question 前40字符
                qt = (qa.get("question", "") or "")[:40]
                dedup_key = f"{field}::{qt}"
                if dedup_key in existing_q_keys:
                    logger.info(
                        "[ClarifyAgent] critical 守卫跳过重复问题: field={!r} question={!r}",
                        field, qt,
                    )
                    continue
                existing_q_keys.add(dedup_key)

                # v2.31: 统一问题结构——用 _build_field_question 构建
                desc = qa.get("missing_desc", field)
                q_data = _build_field_question(field, desc, srr or {})
                question_text = qa.get("question", "") or q_data["question"]
                guarded.append({
                    "id": f"kb_{field.replace('.', '_')}",
                    "field": field,
                    "priority": qa.get("priority", "critical"),
                    "category": "completeness",
                    "question": question_text,
                    "context": q_data["context"],
                    "default_answer": qa.get("missing_desc", ""),
                    "input_type": q_data["input_type"],
                    "options": q_data["options"],
                })
                logger.debug("[ClarifyAgent] critical 守卫补回问题: field={}", field)
        return guarded

    # ========================================================================
    # 冷启动深度问题（docs/01 §4.5 _generate_cold_start_questions）
    # ========================================================================

    def _generate_cold_start_questions(self, srr: dict) -> list[dict]:
        """冷启动模式：生成深度澄清问题（影响范围、边界条件、依赖确认）。

        新产品第一个需求或知识库为空时，没有历史用例可以参考，需要更多人工确认。
        对应 docs/01-requirement-parsing.md §4.5 的 _generate_cold_start_questions。

        v2.13: 检查 SRR 已有内容，已有信息的不再重复提问（遵循"原文为尊"原则）。

        Args:
            srr: SRR JSON 字典

        Returns:
            list[dict]: 冷启动深度问题列表，每项含 category="cold_start" 和 source 标记
        """
        questions: list[dict] = []
        req = srr.get("requirement", {})
        title = req.get("title", "该需求")
        modules = req.get("modules", []) or []
        boundary_conditions = srr.get("boundary_conditions", []) or []
        dependencies = srr.get("dependencies", []) or []
        non_functional = srr.get("non_functional", {}) or {}

        # 1. 模块确认（critical）：模块是知识库隔离和影响分析的基础
        # 已有模块 → 跳过
        if not modules:
            questions.append({
                "priority": "critical",
                "category": "cold_start",
                "source": "cold_start_modules",
                "field": "requirement.modules",
                "question": "这个需求涉及哪些模块？（可多选）例如：auth、payment、order 等",
                "context": "模块是知识库隔离和影响分析的基础，必须明确。",
                "default_answer": "",
                "input_type": "multi_select",
                "options": [
                    "auth（认证/登录/注册）",
                    "user（用户管理）",
                    "order（订单）",
                    "payment（支付/退款）",
                    "product（商品/库存）",
                    "search（搜索/推荐）",
                    "notification（通知/消息）",
                    "admin（后台管理）",
                    "permission（权限/角色）",
                    "其他（请在下方补充）",
                ],
            })

        # 2. 影响范围确认（important）：新产品首个需求无历史用例参考
        # 影响范围通常不在需求文档中描述，总是提问
        questions.append({
            "priority": "important",
            "category": "cold_start",
            "source": "cold_start_impact",
            "field": "impact_scope",
            "question": f"「{title}」这个功能的上线，会影响哪些已有功能？请列出所有受影响的功能模块。",
            "context": "新产品的第一个需求，没有历史用例可以参考，需要明确影响范围以确保测试覆盖。",
            "default_answer": "",
            "input_type": "text",
            "options": [],
        })

        # 3. 边界条件确认（important）：边界条件是 bug 高发区
        # 已有边界条件 → 跳过
        if not boundary_conditions:
            questions.append({
                "priority": "important",
                "category": "cold_start",
                "source": "cold_start_boundary",
                "field": "boundary_conditions",
                "question": "需求的边界条件是什么？比如：最大最小值、空值处理、并发上限、权限边界等。",
                "context": "边界条件往往是 bug 高发区，需要明确以确保测试用例覆盖。",
                "default_answer": "",
                "input_type": "multi_select",
                "options": [
                    "数值范围限制（最大/最小值）",
                    "空值/null 处理",
                    "并发上限/线程安全",
                    "权限边界（越权访问）",
                    "时间/超时限制",
                    "输入长度/格式限制",
                    "状态流转边界",
                    "无特殊边界条件",
                ],
            })

        # 4. 依赖系统确认（optional）：依赖系统异常会影响本功能
        # 已有依赖 → 跳过
        if not dependencies:
            questions.append({
                "priority": "optional",
                "category": "cold_start",
                "source": "cold_start_dependency",
                "field": "dependencies",
                "question": "这个功能依赖哪些外部系统或服务？（如：数据库、第三方 API、消息队列、缓存等）",
                "context": "依赖系统的异常往往会影响本功能，需要确认以设计集成测试。",
                "default_answer": "",
                "input_type": "multi_select",
                "options": [
                    "数据库（MySQL/PostgreSQL）",
                    "缓存（Redis/Memcached）",
                    "消息队列（Kafka/RabbitMQ）",
                    "第三方 API（支付/短信/地图等）",
                    "对象存储（MinIO/OSS/S3）",
                    "搜索引擎（Elasticsearch）",
                    "CDN/静态资源服务",
                    "无外部依赖",
                ],
            })

        return questions

    def _merge_cold_start_questions(
        self,
        questions: list[dict],
        cold_start_questions: list[dict],
    ) -> list[dict]:
        """将冷启动深度问题合并到主问题列表，避免与 LLM 问题重复。

        策略：按 field 去重，冷启动问题仅当同 field 未被 LLM 问题覆盖时才加入。
        合并后重新编号 Q001/Q002...
        """
        existing_fields = {q.get("field") for q in questions if q.get("field")}
        merged = list(questions)
        for cq in cold_start_questions:
            field = cq.get("field", "")
            # impact_scope / boundary_conditions / dependencies 等冷启动专用 field 直接加入
            if field and field not in existing_fields:
                merged.append(cq)
            elif not field:
                merged.append(cq)
        # 重新编号
        for i, q in enumerate(merged, 1):
            q["id"] = q.get("id") or f"Q{i:03d}"
        return merged

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

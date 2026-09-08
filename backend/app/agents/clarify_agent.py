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
# 当 LLM 不可用（如配额耗尽）导致主问题生成降级时，使用此映射表为每个缺口字段生成：
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
        # v2.x: 降级时不填充硬编码通用选项（原为"行业最佳实践标准选项"，与需求无关，属编造）。
        # 遵循"LLM 未返回精炼结果时不兜底"原则，options 留空、改为文本回答，交由用户补充。
        return {
            "question": question,
            "context": context,
            "input_type": "text",
            "options": [],
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

    @staticmethod
    def _filter_questions_by_necessity(questions: list[dict]) -> list[dict]:
        """基于问题必要性的分类过滤（v2.8.66 重写，替代硬数量截断）。

        过滤原则（严格按问题的可信度，不按field数量硬截）：
        ┌────────────────────────────────────────────────────────────────┐
        │ 类别                       │ 判定条件                    │ 保留？ │
        ├────────────────────────────────────────────────────────────────┤
        │ ① category=conflict       │ 字段值category='conflict'     │ ✅ 必留 │
        │ ② 描述不清（ambiguity类） │ category=ambiguity 或         │ ✅ 必留 │
        │                            │ 文本含"未说明/未定义/如何定义/│        │
        │                            │ 请明确/什么是/包含哪些等"     │        │
        │ ③ 字段缺失（completeness  │ category=completeness 且      │ ✅ 可留 │
        │    的真缺口）             │ 文本含"未指定/未说明/缺少/没有│        │
        │                            │ 说明"                        │        │
        │ ④ 猜想上限类             │ field含upper_bound_rule 或    │ ❌ 删除 │
        │   （过度生成重灾区）       │ 文本匹配"超过*怎么办/超出*   │        │
        │                            │ 如何处理/达到上限怎么办"等   │        │
        │ ⑤ 其他                    │ 不匹配以上所有               │ ✅ 可留 │
        └────────────────────────────────────────────────────────────────┘

        说明：
        - 对于 alternate_flows / global_constraints 这种真实存在多个不同子问题的字段，
          不会因"同一个field有3个问题"而误删，只要满足①②③就都保留。
        - ⑤的兜底是避免误删真实问题；若后续发现⑤里还有猜想类可以再补特征。

        Args:
            questions: 问题列表

        Returns:
            list[dict]: 过滤后的问题列表（按原始顺序，只删不加）
        """
        if not questions:
            return questions

        # 描述不清关键词（category=ambiguity之外的兜底）
        # 注意："是否" 二选疑问词不能加，任何选择型问题都有它，会导致猜想型上限问题误判为描述不清
        _AMBIGUITY_KEYWORDS = (
            "未说明", "未定义", "未明确", "请明确", "如何定义", "什么是", "具体指",
            "包含什么", "包括哪些", "范围是什么", "含义", "有没有明确",
        )
        # 真正的字段缺失关键词（completeness但不是猜想）
        _COMPLETENESS_REAL_KEYWORDS = (
            "未指定", "未说明", "缺少", "没有说明", "未给出", "未描述",
        )
        # 猜想上限类：field关键词
        _GUESS_FIELD_MARKERS = (
            "upper_bound_rule",
        )
        # 猜想上限类：文本模式（"超过X怎么办"类）
        _GUESS_TEXT_PATTERNS = (
            # 超过[量词][*]怎么办/如何处理/应如何处理
            re.compile(r"超过[^\?？]{0,30}(怎么办|如何处理|应如何|怎么处理|系统怎么)"),
            re.compile(r"超出[^\?？]{0,30}(怎么办|如何处理|应如何|怎么处理)"),
            re.compile(r"达到(上限|阈值|限制)[^\?？]{0,20}(怎么办|如何处理)"),
            re.compile(r"超过此[^\?？]{0,20}(怎么办|如何处理)"),
        )
        # category=conflict关键词（即便字段写错，也先按conflict信任保留）
        _CONFLICT_TEXT_HINTS = ("矛盾", "冲突", "不一致", "无法同时满足", "又说", "不可达")

        kept: list[dict] = []
        removed: list[dict] = []

        for q in questions:
            q_text = (q.get("question", "") or "").strip()
            category = (q.get("category", "completeness") or "completeness").lower()
            field = (q.get("field", "") or "")
            norm_field = re.sub(r"_\d+$", "", field)

            # ---- ① category=conflict 或明显冲突文本 → 必留 ----
            if category == "conflict" or any(k in q_text for k in _CONFLICT_TEXT_HINTS):
                kept.append(q)
                continue

            # ---- ② 描述不清类（ambiguity） → 必留 ----
            is_ambiguity = (
                category == "ambiguity"
                or any(k in q_text for k in _AMBIGUITY_KEYWORDS)
            )
            if is_ambiguity:
                kept.append(q)
                continue

            # ---- ④ 先判断猜想类（放在completeness之前，避免gap类误留） ----
            is_guess = False
            if any(m in norm_field for m in _GUESS_FIELD_MARKERS):
                is_guess = True
            else:
                for pat in _GUESS_TEXT_PATTERNS:
                    if pat.search(q_text):
                        is_guess = True
                        break
            if is_guess:
                removed.append(q)
                logger.info(
                    "[ClarifyAgent] 过滤猜想上限类问题: id={} field={} question={}",
                    q.get("id", ""), field, q_text[:60],
                )
                continue

            # ---- ③ 真正的completeness字段缺失 → 可留 ----
            is_real_completeness = (
                (category == "completeness" and any(k in q_text for k in _COMPLETENESS_REAL_KEYWORDS))
                or category != "completeness"  # category不为completeness的先信任（gap等）
            )
            if is_real_completeness:
                kept.append(q)
                continue

            # ---- 其他：兜底保留，不误删 ----
            kept.append(q)

        if removed:
            logger.info(
                "[ClarifyAgent] 问题必要性过滤完成: 原始{}条 → 保留{}条（删除{}条猜想型上限问题）",
                len(questions), len(kept), len(removed),
            )
        else:
            logger.debug(
                "[ClarifyAgent] 问题必要性过滤完成: 原始{}条 → 保留{}条（无猜想型删除）",
                len(questions), len(kept),
            )
        return kept

    def _filter_over_broad_llm_questions(self, questions: list[dict], srr: dict) -> list[dict]:
        """过滤 LLM 生成的「过度宽泛的设计阶段细节」追问（v2.8.67 新增，安全网）。

        与 SYSTEM_PROMPT 步骤 2.5 呼应：即便 LLM 未遵守 prompt 约束，也在此兜底删除
        「原始需求未要求的设计阶段细节」类追问，避免打扰用户。只删除、不新增。

        三类删除规则：
        1. 实体字段定义类（data_model 各实体 fields/字段名/类型/约束）：
           除非 raw_text 明确要求数据字典/表结构/字段定义，否则删除。
        2. happy_path 步骤细化类：若 srr.flows.happy_path.steps 已有 >=2 步，
           视为主流程已覆盖，删除「补充详细步骤」类追问。
        3. 逐功能点性能穷举类：若 raw_text 已含性能指标（数值+ms/秒/MB 或 ≤ 数值），
           删除「补充各功能点性能要求」类追问。
        """
        if not questions:
            return questions
        raw_text = (srr.get("_raw_text", "") or "")
        raw_lower = raw_text.lower()
        has_data_dict_req = any(kw in raw_text for kw in (
            "数据字典", "表结构", "字段定义", "字段名", "数据类型", "数据库设计",
            "data model", "schema", "字段列表",
        ))
        flows = srr.get("flows") or {}
        hp = flows.get("happy_path") if isinstance(flows, dict) else None
        hp_steps = (hp or {}).get("steps") if isinstance(hp, dict) else None
        hp_has_content = isinstance(hp_steps, list) and len(hp_steps) >= 2
        has_perf_spec = bool(re.search(r"\d+\s*(ms|毫秒|秒|s\b|mb|gb)|≤\s*\d", raw_lower))

        kept: list[dict] = []
        removed: list[dict] = []
        for q in questions:
            field = (q.get("field", "") or "").lower()
            qt = (q.get("question", "") or "")
            drop = False
            reason = ""
            # 1. 实体字段定义类
            is_entity_fields = "data_model" in field and (
                "field" in field or "字段" in qt or "字段定义" in qt
                or "字段名" in qt or ("类型" in qt and "约束" in qt)
            )
            if is_entity_fields and not has_data_dict_req:
                drop, reason = True, "实体字段定义(raw_text未要求数据字典)"
            # 2. happy_path 步骤细化类
            elif (("happy_path" in field and "step" in field)
                  or ("happy_path" in qt and ("详细步骤" in qt or "具体操作" in qt or "详细操作" in qt))):
                if hp_has_content:
                    drop, reason = True, "happy_path步骤已存在≥2步(raw_text未要求进一步细化)"
            # 3. 逐功能点性能穷举类
            elif ("non_functional" in field and "performance" in field
                  and ("各功能点" in qt or "每个功能" in qt or "补充各" in qt or "各功能" in qt)):
                if has_perf_spec:
                    drop, reason = True, "性能指标raw_text已给出(无需逐功能点穷举)"
            if drop:
                removed.append(q)
                logger.info(
                    "[ClarifyAgent] 过滤过度宽泛追问: id={} field={} reason={} question={}",
                    q.get("id", ""), field, reason, qt[:60],
                )
            else:
                kept.append(q)
        if removed:
            logger.info(
                "[ClarifyAgent] 过度宽泛追问过滤: 原始{}条 -> 保留{}条(删除{}条设计阶段细节)",
                len(questions), len(kept), len(removed),
            )
        return kept

    @staticmethod
    def _validate_and_fix_options(questions: list[dict]) -> list[dict]:
        """校验并修复问题的 options（v2.8.66 新增）。

        LLM 精炼阶段常出现"问题 A 的 options 被错误写成问题 B 的选项"的 index 错乱。
        此函数通过关键词重叠检测：若 options 的关键词与问题 question 文本几乎无重叠，
        则判定为错乱，降级为 input_type=text + options=[]。

        Args:
            questions: 问题列表

        Returns:
            list[dict]: 校验修复后的问题列表
        """
        if not questions:
            return questions

        fixed: list[dict] = []
        fixed_count = 0
        for q in questions:
            q_text = (q.get("question", "") or "").strip()
            opts = q.get("options") or []
            input_type = q.get("input_type", "text") or "text"

            # 仅校验 select 类型且有 options 的问题
            if input_type != "select" or not opts or not q_text:
                fixed.append(q)
                continue

            # 提取 question 的关键词（中文≥2字词 + 英文单词）
            def _extract_tokens(text: str) -> set[str]:
                tokens: set[str] = set()
                # 中文字符：提取2+字连续片段
                for m in re.findall(r"[\u4e00-\u9fa5]{2,}", text):
                    tokens.add(m)
                    # 额外添加2字窗口
                    for i in range(len(m) - 1):
                        tokens.add(m[i:i+2])
                # 英文单词
                for m in re.findall(r"[a-zA-Z][a-zA-Z0-9_]{2,}", text):
                    tokens.add(m.lower())
                return tokens

            q_tokens = _extract_tokens(q_text)
            opts_concat = " ".join(str(o) for o in opts if isinstance(o, (str, int, float)))
            opt_tokens = _extract_tokens(opts_concat)

            # 计算重叠：若 question 非空但与 options 的 token 交集 < 2，且存在否定/冲突关键词，
            # 则判定为 options 错乱（放宽：至少1个重叠或完全不相关才降级）
            overlap = q_tokens & opt_tokens
            is_mismatch = False
            if len(q_tokens) >= 3 and len(opt_tokens) >= 3:
                # 关键词完全不重叠 -> 错乱
                if not overlap:
                    is_mismatch = True
                else:
                    # 启发式：问题中提到的核心词在 options 里完全没有
                    core_terms = [
                        # 时间类
                        "秒", "分钟", "小时", "天", "当日", "次日", "次", "小时内",
                        # 动作类
                        "发送", "验证码", "登录", "密码", "删除", "修改", "重置", "绑定",
                        # 数字阈值类
                        "次", "个", "条", "≥", ">", "<=", "限制", "阈值", "上限",
                    ]
                    q_core = {t for t in core_terms if t in q_text}
                    opt_core = {t for t in core_terms if t in opts_concat}
                    if q_core and not (q_core & opt_core):
                        # 问题的核心动作/量词完全不在 options 中
                        is_mismatch = True

            if is_mismatch:
                q2 = dict(q)
                q2["input_type"] = "text"
                q2["options"] = []
                q2["_options_fixed_reason"] = f"LLM options与问题不匹配，降级为text（重叠={len(overlap)}）"
                fixed.append(q2)
                fixed_count += 1
                logger.info(
                    "[ClarifyAgent] options不匹配已降级: id={} field={} question={}",
                    q2.get("id", ""), q2.get("field", ""), q_text[:50],
                )
            else:
                fixed.append(q)

        if fixed_count > 0:
            logger.info("[ClarifyAgent] options校验完成: 共修复{}条（LLM options index错乱）", fixed_count)
        return fixed

    async def clarify(
        self,
        srr: dict,
        session = None,
        *,
        progress_callback = None,
    ) -> dict:
        """对 SRR 执行需求澄清。

        v2.8.52: 不再长持有外部 session，内部 DB 查询使用短生命周期 session，
        避免 LLM 调用期间占用连接池。

        v2.8.57: 新增 progress_callback 参数，用于在 LLM 调用级上报进度。
            签名: async def callback(message: str, progress: int) -> None
            progress 取值范围 40-70（kb_search 阶段内部细分）：
                41 KB 状态检查
                44 优化需求检测（规则，v3.1.5 起不再调 LLM）
                48 KB 查询生成（规则，v3.1.5 起不再调 LLM）
                52 KB 检索 + 答案校验 (LLM, 分批)
                60 KB 答案应用 + 字段冲突转人工（v3.1.5 起不再 LLM 裁决）
                66 待澄清问题选项规则化（v3.1.5 起不再调 LLM）
                70 澄清完成
            callback 异常会被捕获并降级为日志，不中断主流程。

        v3.1.5 提示词精简：LLM 调用从 6 次收敛为 2 次——
            保留 主问题生成（SYSTEM_PROMPT，含字段冲突检测+每题必带 options）
            保留 KB 答案校验（KB_ANSWER_VERIFIER_SYSTEM_PROMPT）
            其余（KB 查询生成/选项精炼/冲突裁决/优化检测）改为规则实现。

        Args:
            srr: SRR JSON 字典
            session: 已废弃（保留签名兼容性，不再使用）
            progress_callback: 可选的进度回调（v2.8.57 新增）

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

        # v2.8.57: 进度回调封装（异常降级为日志，不中断主流程）
        async def _report(message: str, progress: int) -> None:
            if progress_callback is None:
                return
            try:
                await progress_callback(message, progress)
            except Exception as cb_err:
                logger.debug("[ClarifyAgent] 进度回调失败（已降级）: progress={}, err={}",
                            progress, str(cb_err)[:120])

        # 读取知识库专属 LLM 供应商（若已设置），KB 相关 LLM 调用使用该供应商
        # v2.8.52: 使用内部短生命周期 session，不长持有外部连接
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
        kb_empty, kb_empty_err = await asyncio.to_thread(self._safe_is_empty)
        _d1 = int((time.time() - _t0) * 1000)
        product_first, product_first_err = await asyncio.to_thread(self._safe_is_product_first, product_id)
        _d2 = int((time.time() - _t0) * 1000) - _d1
        # v2.8.55 Bug C-2: 区分真空与故障——KB 不可用时不触发冷启动
        kb_unavailable = kb_empty_err or product_first_err
        cold_start = (not kb_unavailable) and (kb_empty or product_first)
        logger.info("[ClarifyAgent] KB 状态: kb_empty={}, product_first={}, kb_unavailable={}, cold_start={}, product_id={}",
                    kb_empty, product_first, kb_unavailable, cold_start, product_id)
        kb_access_log.append({
            "action": "check_kb_status",
            "type": "check_kb_status",
            "label": "KB 状态检查",
            "query": "知识库中是否存在同类需求可以辅助分析当前需求？是否需要启用冷启动模式？",
            "query_detail": (
                f"全局向量库{'不可用' if kb_unavailable else ('为空' if kb_empty else '有数据（含其他产品）')}，"
                f"当前产品{'不可用' if kb_unavailable else ('无历史需求（冷启动）' if product_first else '已有历史需求')}"
                if not kb_unavailable
                else "向量库不可用"
            ),
            "result_count": 0 if (kb_unavailable or kb_empty or product_first) else 1,
            "product_first_requirement": product_first,
            "duration_ms": _d1 + _d2,
            "status": "error" if kb_unavailable else "success",
        })
        await _report("KB 状态检查完成，正在检测需求类型（优化/新建）...", 41)

        # ② 规则检测：完整性 + 歧义性
        # v3.1.6: 优化需求专项检测整个移除（用户裁定多余）——不管需求类型直接查 KB，
        # 查不到返回空不影响流程；优化类特有的「历史数据影响」问题由主问题生成的
        # 语义检测覆盖（优化类需求原文必然涉及存量数据，LLM 会自然生成该问题）
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

        # ②b PRD 专项检测（v2.8.64 新增；v3.1.6 起对所有需求执行——
        # 优化需求检测已移除，不再区分新建/优化路径。5 项规则层前置检测：
        # a)上限落地规则 b)降级触发/恢复 c)风险能力与权限一致性 d)立场不一致 e)模糊条件判定
        # 结果分别注入 completeness.important_missing（gap类）和 ambiguities（ambiguity/conflict类）
        # 并为 completeness 新增 prd_missing / prd_ambiguities 键，供 build_user_prompt 展示给 LLM）
        if True:
            try:
                prd_gaps, prd_ambg = self._detect_prd_specific_gaps(srr)
                if prd_gaps:
                    # 保存到 completeness，供下游展示
                    completeness["prd_missing"] = prd_gaps
                    for gap in prd_gaps:
                        completeness["important_missing"].append({
                            "field": gap["field"],
                            "description": f"[PRD专项检测] {gap['description']} | 证据: {gap.get('evidence','')[:80]}",
                            "_suggested_question": gap.get("suggested_question", ""),
                            "_suggested_options": gap.get("suggested_options", []),
                        })
                if prd_ambg:
                    completeness["prd_ambiguities"] = prd_ambg
                    for amb in prd_ambg:
                        cat = amb.get("category", "ambiguity")
                        # 矛盾类加到 conflicts，歧义类加到 ambiguities（保持和 ambiguities 机制一致）
                        if cat == "conflict":
                            ambiguities.append({
                                "type": "prd_stance_conflict",
                                "field": amb["field"],
                                "description": amb["description"],
                                "evidence": amb.get("evidence", ""),
                                "_suggested_question": amb.get("suggested_question", ""),
                                "_suggested_options": amb.get("suggested_options", []),
                            })
                        else:
                            ambiguities.append({
                                "type": "prd_ambiguity",
                                "field": amb["field"],
                                "description": amb["description"],
                                "evidence": amb.get("evidence", ""),
                                "_suggested_question": amb.get("suggested_question", ""),
                                "_suggested_options": amb.get("suggested_options", []),
                            })
                # 规则层记录：因为 ambiguities 原本为空列表，现在有了，日志里就能看到
                if prd_gaps or prd_ambg:
                    logger.info(
                        "[ClarifyAgent] 规则检测后追加 PRD 专项检测: important_missing+={}, ambiguities+={}",
                        len(prd_gaps), len(prd_ambg),
                    )
            except Exception as e:
                # PRD 专项检测失败不阻断主流程
                logger.warning(
                    "[ClarifyAgent] PRD 专项检测异常（已降级跳过，不影响主流程）: {}",
                    str(e)[:200],
                )

        # ②c v2.8.62: LLM 基于原始文本进行语义检测（前移自 ⑧c）
        # 用户要求：LLM 分析原始文本找问题应先于 KB 查询，让 KB 有机会解决 LLM 发现的问题
        # _generate_questions 基于 _raw_text 检测跨节矛盾/引用未定义/信息缺失/模糊表述
        llm_semantic_questions: list[dict] = []
        try:
            llm_result = await self._generate_questions(
                srr=srr,
                completeness=completeness,
                ambiguities=ambiguities,
                similar_reqs=[],
                augmentations=[],
            )
            # v2.8.66: LLM 语义问题生成后立即做必要性过滤——猜想型 upper_bound_rule 不进入后续分流和KB检索
            llm_semantic_questions = self._filter_questions_by_necessity(
                llm_result.get("questions", []) or []
            )
            # v2.8.69 防御性兜底：LLM 语义层或任意上游转换阶段万一仍有 theme_grp_N
            # 内部变量泄漏（如 LLM output 中意外含），扫描 question/context 统一替换为
            # 「引用的规则」，避免用户看到机械的内部标识符。同时对规则层标准格式
            # 【xxx】引用未定义：「YYY」——若 YYY 仍含 theme_grp 标识，替换为空描述，
            # 并触发基于上下文的主题抽取。
            import re as _re_leak
            _LEAK_PAT = _re_leak.compile(r"theme_grp_[0-9]+")
            _patched = 0
            for _q in llm_semantic_questions:
                _qstr = str(_q.get("question", "") or "")
                _cstr = str(_q.get("context", "") or "")
                if _LEAK_PAT.search(_qstr) or _LEAK_PAT.search(_cstr):
                    _q["question"] = _LEAK_PAT.sub("引用的规则", _qstr)
                    _q["context"] = _LEAK_PAT.sub("引用的规则", _cstr)
                    _patched += 1
                    # 顺手填充 _ref_theme_label = "引用的规则" 兜底，避免 KB short_query 为空
                    if not _q.get("_ref_theme_label"):
                        _q["_ref_theme_label"] = "引用的规则"
            if _patched:
                logger.warning(
                    "[ClarifyAgent] v2.8.69 兜底防泄漏：LLM语义问题发现 theme_grp_N 泄漏 {} 条，已替换为通用主题名",
                    _patched,
                )
            if llm_semantic_questions:
                logger.info(
                    "[ClarifyAgent] LLM 语义检测（前置）生成问题: {} 个",
                    len(llm_semantic_questions),
                )
                # LLM 问题 id 加前缀避免与 KB 问题 id 冲突
                for q in llm_semantic_questions:
                    if q.get("id"):
                        q["id"] = f"llm_{q['id']}"
            await _report(f"LLM 语义检测完成（发现 {len(llm_semantic_questions)} 个问题），正在生成知识库查询...", 46)
        except Exception as e:
            # LLM 语义检测失败不阻断流水线——规则层和 KB 仍可用
            logger.warning(
                "[ClarifyAgent] LLM 语义检测失败（不阻断，仅跳过）: {}",
                str(e)[:200],
            )
            await _report("LLM 语义检测失败，继续生成知识库查询...", 46)

        # v2.8.65: LLM 语义问题分流--确保「先 KB 查询，未答的才人工澄清」
        # 之前的问题：llm_semantic_questions 在步骤⑧c 直接 extend 进最终问题列表，
        # 完全绕过 KB 查询。现在按 category 分流：
        #   - completeness/ambiguity -> 转为额外 KB 查询，让 KB 有机会回答（如「现有图片上传规则」
        #     这类引用未定义问题，KB 中可能已有定义）
        #   - conflict -> 文档内部矛盾，KB 无法解决，直接人工澄清
        # 转为 KB 查询的问题在步骤⑧ 会随 KB 未答问题一起转为人工问题（kb_ 前缀），
        # 步骤⑧c 只合并 conflict 类型的 LLM 问题。
        llm_kb_eligible_questions: list[dict] = []  # 适合 KB 查询的（completeness/ambiguity）
        llm_direct_questions: list[dict] = []        # 直接人工的（conflict，KB 无法解决）
        if llm_semantic_questions:
            # v2.8.73 收敛修复：LLM 语义层生成的「引用未定义」问题也需跳过已在澄清历史中
            # 回答过的主题（与规则层 _detect_logical_contradictions 的收敛逻辑保持一致）。
            # 根因：修复前 LLM 语义层每轮都会重新生成「图片上传/版本保留/敏感词」等引用未定义
            # 问题并走 KB→人工，导致第 2/3 轮仍重复已答主题，澄清 3 轮才强制放行。
            # 修复：读取 _clarify_history 已展示问题的 _ref_theme_label，收集已答主题，
            # 对 LLM 层引用未定义类问题做主题重叠判断，命中已答主题则跳过。
            _llm_resolved_themes: set[str] = set()
            try:
                _hist = srr.get("_clarify_history") if isinstance(srr, dict) else None
                if isinstance(_hist, list):
                    for _entry in _hist:
                        if not isinstance(_entry, dict):
                            continue
                        for _q in (_entry.get("questions") or []):
                            if not isinstance(_q, dict):
                                continue
                            _tl = _q.get("_ref_theme_label") or ""
                            if isinstance(_tl, str) and _tl:
                                _llm_resolved_themes.add(_tl)
            except Exception:
                _llm_resolved_themes = set()

            _LLM_KNOWN_THEMES = (
                "图片上传", "上传规则", "素材上传", "文件上传", "附件上传",
                "敏感词校验", "敏感词", "权限控制", "访问控制", "角色权限",
                "版本保留", "版本管理", "改动比例", "小修订", "审批流程",
                "淘汰规则", "限流规则", "自动保存", "归档规则",
            )

            def _llm_theme_overlaps(a: str, b: str) -> bool:
                if not a or not b:
                    return False
                if a == b:
                    return True
                _ak = {k for k in _LLM_KNOWN_THEMES if k in a}
                _bk = {k for k in _LLM_KNOWN_THEMES if k in b}
                return bool(_ak & _bk)

            def _llm_is_ref_undef_q(q: dict) -> bool:
                _qt = str(q.get("question", "") or "")
                _ctx = str(q.get("context", "") or "")
                _hay = f"{_qt} {_ctx}"
                return ("引用未定义" in _ctx
                        or "沿用" in _hay
                        or ("现有" in _hay and ("规则" in _hay or "一致" in _hay)))

            def _llm_extract_theme(q: dict) -> str:
                # 优先取 _ref_theme_label；否则从问题「」中抽取主题，否则取已知主题词
                _tl = q.get("_ref_theme_label") or ""
                if isinstance(_tl, str) and _tl:
                    return _tl
                _qt = str(q.get("question", "") or "")
                _ctx = str(q.get("context", "") or "")
                _hay = f"{_qt} {_ctx}"
                for _kw in _LLM_KNOWN_THEMES:
                    if _kw in _hay:
                        return _kw
                import re as _re_th
                _m = _re_th.search(r"「([^」]{1,20})」", _qt or _ctx)
                if _m:
                    return _m.group(1)
                return (_qt or _ctx)[:20].strip()

            # v2.8.66: 过滤与固定优化问题重复的 legacy_data_impact 专项字段
            # 固定问题 kb_optimization_impact + kb_optimization_impact_scope 已覆盖用户对
            # 迁移/回滚/影响范围的提问入口，无需 LLM 语义检测再额外生成专项字段提问
            # v2.8.67: 额外过滤语义重复——LLM 可能未用 legacy_data_impact.* 作 field，
            # 但 question 中含"优化/改进+历史数据+影响"，与固定配套问题本质相同，也需删除
            _LDI_DUP_FIELDS = (
                "legacy_data_impact.impact_scope",
                "legacy_data_impact.migration_plan",
                "legacy_data_impact.rollback_and_validation",
                "legacy_data_impact.risks",
                "legacy_data_impact.user_clarification",
                "legacy_data_impact",
                "optimization_impact",
            )

            # v3.0.1: 追加兜底收集——对无 _ref_theme_label 的历史问题，
            # 只要是引用未定义类型，用 _llm_extract_theme 提取主题加入 _llm_resolved_themes
            # 否则会导致 LLM 语义层问题在第一轮回答后第二轮仍重复提问
            try:
                _hist = srr.get("_clarify_history") if isinstance(srr, dict) else None
                if isinstance(_hist, list):
                    for _entry in _hist:
                        if not isinstance(_entry, dict):
                            continue
                        for _q in (_entry.get("questions") or []):
                            if not isinstance(_q, dict):
                                continue
                            if _q.get("_ref_theme_label"):
                                continue  # 已有 _ref_theme_label，前面已经收集过了
                            if _llm_is_ref_undef_q(_q):
                                _fallback = _llm_extract_theme(_q)
                                if _fallback:
                                    _llm_resolved_themes.add(_fallback)
            except Exception:
                pass  # 兜底不影响主流程
            for q in llm_semantic_questions:
                cat = q.get("category", "completeness")
                field = q.get("field", "") or ""
                qtext = (q.get("question", "") or "")[:100]
                # v2.8.73 收敛：LLM 层引用未定义类问题命中已答主题则跳过本轮
                if _llm_resolved_themes and _llm_is_ref_undef_q(q):
                    _q_theme = _llm_extract_theme(q)
                    if any(_llm_theme_overlaps(_q_theme, _rt) for _rt in _llm_resolved_themes):
                        logger.info(
                            "[ClarifyAgent] 收敛跳过 LLM 层已答引用未定义主题: theme={}",
                            _q_theme,
                        )
                        continue
                # v2.8.66: 跳过 LLM 生成的 legacy_data_impact 专项字段（与固定优化问题重复）
                if any(f in field for f in _LDI_DUP_FIELDS):
                    logger.info(
                        "[ClarifyAgent] 跳过 LLM 生成的 legacy_data_impact 专项问题(与固定优化问题重复): field={}",
                        field,
                    )
                    continue
                # v2.8.67: 语义级去重——问题文本与"优化是否影响历史数据"或"影响范围描述"核心相同
                _is_opt_dup_q1 = (
                    "优化" in qtext and "历史数据" in qtext and "影响" in qtext
                ) or (
                    "改进" in qtext and "历史数据" in qtext and "影响" in qtext
                )
                _is_opt_dup_q2 = (
                    "影响范围" in qtext and "数据" in qtext
                ) or (
                    "描述" in qtext and "历史数据" in qtext and "实体" in qtext
                )
                if _is_opt_dup_q1 or _is_opt_dup_q2:
                    logger.info(
                        "[ClarifyAgent] 跳过 LLM 生成的优化影响语义重复问题: qtext[:60]={}",
                        qtext[:60],
                    )
                    continue
                if cat == "conflict":
                    llm_direct_questions.append(q)
                else:
                    llm_kb_eligible_questions.append(q)
            if llm_kb_eligible_questions or llm_direct_questions:
                logger.info(
                    "[ClarifyAgent] LLM 语义问题分流: {} 个转 KB 查询, {} 个直接人工(conflict)",
                    len(llm_kb_eligible_questions), len(llm_direct_questions),
                )

        # ====================================================================
        # v2.8.67 统一问题分流池（核心：所有问题源先汇入，再按同一规则决定「先 KB」或「直接人工」）
        #   - 直接人工（conflict / 文档内部自相矛盾）：KB 无法解决，立即人工
        #   - 其余所有（completeness / ambiguity / gap / impact_analysis / ref_undef 等）：
        #     一律走 KB，查不到才转人工（KB 路径会为人工问题自动补全建议答案 options）
        # ====================================================================
        _UNIFIED_CANDIDATES: list[dict] = []

        # 来源 B：v2.8.65 规则层逻辑矛盾检测（_detect_logical_contradictions）
        # 之前这里直接 extend 人工（原 L1114）→ 现在并入统一池：
        #   - contradiction / conflict：直接人工（KB 无法解决文档内部"50个 vs 不设上限"自相矛盾）
        #   - ref_undef / ambiguity / completeness：先走 KB！例如「沿用现有图片上传规则」这种引用未定义，
        #     如果历史 KB 已归档，hybrid_searcher 很可能直接查到，不用打扰用户
        _UNIFIED_CANDIDATES.extend((self._detect_logical_contradictions(srr) or []))

        # v2.8.68 同主题引用未定义去重（修复 LLM 层 completeness 与规则层
        #   ambiguity 类问题重复出现的问题，如"沿用现有图片上传规则"被两边各问1次）。
        # 原理：基于「引用未定义信号 + 主题关键词重合」判断语义重复，保留1条。
        # 优先级：规则层的标准格式 > LLM层自由格式（因为规则层有context/来源、主题明确）
        try:
            def _is_ref_undef(q: dict) -> bool:
                q_text = str(q.get("question", "") or "")
                q_ctx = str(q.get("context", "") or "")
                return ("引用未定义" in q_ctx
                        or "沿用" in f"{q_text} {q_ctx}"
                        or ("现有" in f"{q_text} {q_ctx}"
                            and ("规则" in f"{q_text} {q_ctx}" or "一致" in f"{q_text} {q_ctx}")))

            def _ref_undef_theme_key(q: dict) -> str:
                q_text = str(q.get("question", "") or "")
                q_ctx = str(q.get("context", "") or "")
                hay = f"{q_text} {q_ctx}"
                # 从问题中抽取 「XXX」 包裹的主题，或命中已知主题词
                known_themes = [
                    "图片上传", "上传规则", "素材上传", "文件上传", "附件上传",
                    "敏感词校验", "敏感词", "权限控制", "访问控制", "角色权限",
                    "版本保留", "版本管理", "改动比例", "小修订", "审批流程",
                    "淘汰规则", "限流规则", "自动保存", "归档规则",
                ]
                hits = [kw for kw in known_themes if kw in hay]
                if hits:
                    return "|".join(sorted(set(hits)))
                # fallback：中文长词抽取(4-8字)
                import re as _re
                m = _re.search(r"「([^」]{1,16})」", q_text)
                if m:
                    return m.group(1)
                return q_text[:24].strip()

            rule_indices = set()
            theme_bucket: dict[str, list[int]] = {}
            for i, q in enumerate(_UNIFIED_CANDIDATES):
                if not _is_ref_undef(q):
                    continue
                # 规则层的 ref_undef 带"引用未定义"context字段，优先级更高
                if "引用未定义" in str(q.get("context", "")):
                    rule_indices.add(i)
                tk = _ref_undef_theme_key(q)
                theme_bucket.setdefault(tk, []).append(i)

            dedup_skip: set[int] = set()
            for tk, idxs in theme_bucket.items():
                if len(idxs) <= 1:
                    continue
                # 优先保留规则层(rule_indices)的条目；否则保留第一个
                keep = None
                for j in idxs:
                    if j in rule_indices:
                        keep = j
                        break
                if keep is None:
                    keep = idxs[0]
                for j in idxs:
                    if j != keep:
                        dedup_skip.add(j)

            if dedup_skip:
                before = len(_UNIFIED_CANDIDATES)
                _UNIFIED_CANDIDATES = [
                    q for i, q in enumerate(_UNIFIED_CANDIDATES) if i not in dedup_skip
                ]
                logger.info(
                    "[ClarifyAgent] 统一分流池-引用未定义同主题去重: 原 {} 条 → 现 {} 条, 跳过 {} 条",
                    before, len(_UNIFIED_CANDIDATES), len(dedup_skip),
                )
        except Exception as _dedup_e:
            logger.warning(
                "[ClarifyAgent] 引用未定义同主题去重异常（安全降级不影响主流程）: {}",
                str(_dedup_e)[:200],
            )

        # 统一分流：category == "conflict" 或含 "contradiction" → 直接人工
        #   - v2.8.67 新增：_detect_optimization_impact 产出的两个配套问题（kb_optimization_impact
        #     + kb_optimization_impact_scope）标记为「_direct_human_tail」→ 只在人工澄清显示，
        #     排到问题列表末尾，不走 KB 查询（因为 KB 无法回答"当前需求是否影响历史数据"这类
        #     与特定需求强绑定的判断性问题；而且 KB 中也不会有该用户项目的历史数据情况）
        #   - 其余 impact_analysis / ambiguity / completeness 等：一律先 KB
        _DIRECT_CATS = {"conflict", "contradiction"}
        _OPT_TAIL_IDS = {"kb_optimization_impact", "kb_optimization_impact_scope"}
        for q in _UNIFIED_CANDIDATES:
            q["_from_unified_candidate"] = True
            qid = (q.get("id") or "")
            cat = (q.get("category") or "").lower()
            if qid in _OPT_TAIL_IDS:
                # 固定优化影响配套问题 → 直接人工 + 末位插队标记（步骤⑧c 后单独追加到末尾）
                q["_direct_human_tail"] = True
                llm_direct_questions.append(q)
            elif cat in _DIRECT_CATS or "contradict" in cat:
                llm_direct_questions.append(q)
            else:
                llm_kb_eligible_questions.append(q)
        if _UNIFIED_CANDIDATES:
            _direct = sum(1 for q in _UNIFIED_CANDIDATES
                          if ((q.get("category") or "").lower() in _DIRECT_CATS
                              or "contradict" in (q.get("category") or "").lower()))
            logger.info(
                "[ClarifyAgent] 统一分流池: 总 {} 条(优化专项+规则层矛盾检测), "
                "其中 {} 条先 KB, {} 条直接人工(conflict/contradiction)",
                len(_UNIFIED_CANDIDATES), len(_UNIFIED_CANDIDATES) - _direct, _direct,
            )

        # v2.8.68 第二轮：对 llm_kb_eligible_questions 中的引用未定义做同主题去重
        # （因为 LLM 语义层生成的引用未定义不经过统一池 _UNIFIED_CANDIDATES，直接在
        #  llm_kb_eligible_questions 中；而规则层 ref_undef 是先入 _UNIFIED_CANDIDATES
        #  再被分流 append 到 llm_kb_eligible_questions。所以第一轮只能合并统一池内的
        #  规则层间重复，第二轮合并 LLM 层与规则层的跨来源重复）。
        # 采用「多主题维度投桶 → 每个桶保留1条（规则层优先）→ 其余命中任意共同主题桶即跳过」
        # 的策略，容忍规则层标准格式问题（常含"引用未定义：图片上传...同时保持敏感词校验不变"
        # 这种合并了多主题的情况）和 LLM 层单主题问题形成的主题集合不完全相等的情况。
        try:
            def _is_ref_undef_v2(q: dict) -> bool:
                q_text = str(q.get("question", "") or "")
                q_ctx = str(q.get("context", "") or "")
                hay = f"{q_text} {q_ctx}"
                return ("引用未定义" in q_ctx
                        or "沿用" in hay
                        or ("现有" in hay and ("规则" in hay or "一致" in hay)))

            _THEMES_V2: tuple[str, ...] = (
                "图片上传", "上传规则", "素材上传", "文件上传", "附件上传",
                "敏感词校验", "敏感词过滤", "敏感词", "权限控制", "访问控制",
                "角色权限", "版本保留", "版本管理", "改动比例", "小修订",
                "审批流程", "淘汰规则", "限流规则", "自动保存", "归档规则",
            )
            import re as _re_v2

            def _theme_sig_v2(q: dict) -> set[str]:
                q_text = str(q.get("question", "") or "")
                q_ctx = str(q.get("context", "") or "")
                hay = f"{q_text} {q_ctx}"
                sig: set[str] = set()
                for kw in _THEMES_V2:
                    if kw in hay:
                        sig.add(kw)
                if not sig:
                    m2 = _re_v2.search(r"「([^」]{1,20})」", q_text)
                    if m2:
                        sig.add(m2.group(1))
                if not sig:
                    sig.add(q_text[:20].strip())
                return sig

            rule_priority: list[int] = []
            ref_undef_idx: list[int] = []
            sigs: dict[int, set[str]] = {}
            for i, q in enumerate(llm_kb_eligible_questions):
                if not _is_ref_undef_v2(q):
                    continue
                ref_undef_idx.append(i)
                if "引用未定义" in str(q.get("context", "")):
                    rule_priority.append(i)
                sigs[i] = _theme_sig_v2(q)

            # 主题桶：每个主题词 -> 落入该主题的所有引用未定义下标
            per_theme_bucket: dict[str, list[int]] = {}
            for i in ref_undef_idx:
                for t in sigs[i]:
                    per_theme_bucket.setdefault(t, []).append(i)

            # 每个主题桶内：优先选 rule_priority 的条目为 keep，其余作为 skip 候选
            theme_keep: dict[str, int] = {}
            skip_candidates: set[int] = set()
            for t, idxs in per_theme_bucket.items():
                if len(idxs) <= 1:
                    theme_keep[t] = idxs[0]
                    continue
                keep_t = None
                for j in idxs:
                    if j in rule_priority:
                        keep_t = j
                        break
                if keep_t is None:
                    keep_t = min(idxs)
                theme_keep[t] = keep_t
                for j in idxs:
                    if j != keep_t:
                        skip_candidates.add(j)

            # 最终要跳过的：在所有共同主题桶中，它都不是 keep 者
            skip2: set[int] = set()
            for j in skip_candidates:
                j_themes = sigs.get(j, set())
                # 如果 j 命中的任一主题桶保留了它（它是该桶 keep）就不跳过
                still_keep = any(theme_keep.get(t) == j for t in j_themes)
                if not still_keep:
                    skip2.add(j)

            if skip2:
                before = len(llm_kb_eligible_questions)
                llm_kb_eligible_questions = [
                    q for i, q in enumerate(llm_kb_eligible_questions) if i not in skip2
                ]
                logger.info(
                    "[ClarifyAgent] 引用未定义跨来源去重(LLM语义 vs 规则层): 原 {} 条 → 现 {} 条, 跳过 {} 条",
                    before, len(llm_kb_eligible_questions), len(skip2),
                )
        except Exception as _dedup_e2:
            logger.warning(
                "[ClarifyAgent] 引用未定义跨来源去重异常（安全降级不影响主流程）: {}",
                str(_dedup_e2)[:200],
            )

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

        # v2.8.55: 移除冗余的全局相似检索和 PG 关系增强
        # 原因：这两个检索结果未被 _query_kb_for_answers 实际使用，仅作为日志展示
        #       真实的 KB 问答在 _query_kb_for_answers 内部独立调用 hybrid_searcher
        #       hybrid_search 已支持模块过滤 + 时间衰减排序，无需前置冗余检索
        #       删除后每次 Clarify 节省 ~800ms Milvus 检索 + 6 次 PG 查询
        # v2.8.59: 彻底清理残留的空变量和 kb_access_log 占位条目

        _t3 = time.time()
        # ③ KB 查询（v3.1.6 合并到问题生成）：KB 查询不再独立生成。
        # 主问题生成（SYSTEM_PROMPT）为每个问题直接给出 kb_query 短关键词检索词；
        # conflict 类（文档内部矛盾）不查 KB 直接人工；规则缺口由 _fallback_gap_queries 兜底。
        kb_questions: list[dict] = []

        # v2.8.65: 将 LLM 语义检测中「适合 KB 查询」的问题转为 KB 查询
        # 这些问题（如「现有图片上传规则是什么」「自动保存保留策略是什么」）很可能在 KB
        # 历史需求中已有定义，应先查 KB，未答的才在步骤⑧ 转为人工澄清问题。
        # field 加 llm_semantic. 前缀标记来源，便于步骤⑧c 去重时识别。
        # v2.8.67: ref_undef 类问题用短主题关键词检索——长句做 BM25 分词后核心词被稀释，
        #   导致 KB 查不到明明有的内容。典型案例：图片上传规则的长question零命中，
        #   换"图片上传规则 格式 大小"即命中。v3.1.6 起优先用问题自带的 kb_query 字段。
        if llm_kb_eligible_questions:
            llm_derived_queries: list[dict] = []
            for q in llm_kb_eligible_questions:
                orig_field = q.get("field", "") or "semantic_gap"
                derived_field = orig_field if orig_field.startswith("llm_semantic.") else f"llm_semantic.{orig_field}"
                question_text = q.get("question", "") or ""
                if not question_text:
                    continue
                # v3.1.6: 优先用主问题生成给出的 kb_query（上下文感知、主题聚焦）
                # v2.8.67: kb_query 为空时兜底用 ref_undef 主题标签追加常见检索词
                short_query: str | None = None
                kbq = str(q.get("kb_query", "") or "").strip()
                if kbq:
                    short_query = kbq[:60]
                theme_label = q.get("_ref_theme_label") or ""
                if not short_query and theme_label and isinstance(theme_label, str) and len(theme_label) <= 40:
                    # 根据主题追加常见检索词，提升召回
                    suffixes: list[str] = []
                    if any(tk in theme_label for tk in ["图片", "上传", "素材"]):
                        suffixes = ["格式", "大小", "限制"]
                    elif any(tk in theme_label for tk in ["版本", "修订", "改动比例"]):
                        suffixes = ["判定标准", "阈值", "计算方式"]
                    elif any(tk in theme_label for tk in ["权限", "角色"]):
                        suffixes = ["角色", "操作", "访问控制"]
                    elif any(tk in theme_label for tk in ["保存", "自动保存"]):
                        suffixes = ["保留数量", "上限", "策略"]
                    short_query = theme_label + (" " + " ".join(suffixes) if suffixes else "")
                    logger.debug(
                        "[ClarifyAgent] ref_undef 类生成短 KB 查询: theme={} short={}",
                        theme_label, short_query,
                    )
                derived = {
                    "field": derived_field,
                    "missing_desc": q.get("context", "") or q.get("default_answer", "") or orig_field,
                    "priority": q.get("priority", "important"),
                    "question": question_text,
                    "_is_baseline": False,
                    "_from_llm_semantic": bool(q.get("_from_llm_semantic")),
                    "_from_unified_candidate": bool(q.get("_from_unified_candidate")),
                }
                if short_query:
                    derived["_kb_short_query"] = short_query
                llm_derived_queries.append(derived)
            if llm_derived_queries:
                _existing_keys = {f"{q.get('field','')}::{q.get('question','')[:40]}" for q in kb_questions}
                _added = 0
                for dq in llm_derived_queries:
                    _dk = f"{dq['field']}::{dq['question'][:40]}"
                    if _dk in _existing_keys:
                        continue
                    kb_questions.append(dq)
                    _existing_keys.add(_dk)
                    _added += 1
                logger.info(
                    "[ClarifyAgent] LLM 语义问题转 KB 查询: {} 个（去重后新增 {}）",
                    len(llm_derived_queries), _added,
                )

        # v3.1.6 规则兜底：LLM 问题未覆盖的规则缺口（critical/important missing +
        # baseline gaps）仍须查 KB，防止缺口静默丢失。已由 LLM 问题覆盖的缺口（按
        # field 匹配）不再重复生成查询。
        covered_fields = {
            str(q.get("field", "") or "").removeprefix("llm_semantic.")
            for q in llm_semantic_questions
        }
        try:
            fallback_queries = self._fallback_gap_queries(
                completeness, srr.get("_is_incremental", False), covered_fields, srr=srr
            )
            if fallback_queries:
                _fk = {f"{q.get('field','')}::{q.get('question','')[:40]}" for q in kb_questions}
                _fadded = 0
                for fq in fallback_queries:
                    _fdk = f"{fq['field']}::{fq['question'][:40]}"
                    if _fdk in _fk:
                        continue
                    kb_questions.append(fq)
                    _fk.add(_fdk)
                    _fadded += 1
                logger.info(
                    "[ClarifyAgent] 规则兜底缺口转 KB 查询: {} 个（去重后新增 {}）",
                    len(fallback_queries), _fadded,
                )
            kb_questions = self._filter_questions_by_necessity(kb_questions)
        except Exception as fb_err:
            logger.warning("[ClarifyAgent] 规则兜底 KB 查询失败（不阻断）: {}", str(fb_err)[:150])
        await _report(f"知识库查询准备完成（{len(kb_questions)} 条），正在检索同类需求并校验答案...", 48)

        # v2.8.73 N4 优化：合并同主题细分 KB 查询，提升命中率。
        # 根因：LLM 语义层会为同一主题生成多个细粒度查询（如「图片上传」的格式/超限提示/批量
        # 拆成 3 条），每条要求历史文档未覆盖的细节 → 被 LLM 校验器判定"信息不充分"整体拒绝，
        # 导致命中率低（实测 3/9）。合并后保留 1 条主题级查询，让 KB 一次命中核心规则。
        try:
            _MERGE_THEME_KWS: tuple[tuple[str, ...], ...] = (
                ("图片上传", "上传规则", "素材上传", "文件上传", "附件上传"),
                ("敏感词校验", "敏感词", "敏感词过滤"),
                ("权限控制", "访问控制", "角色权限"),
                ("版本保留", "版本管理", "改动比例", "小修订", "自动保存"),
            )
            def _llm_merge_theme(q: dict) -> str | None:
                _hay = f"{q.get('question','')} {q.get('missing_desc','')}"
                for kws in _MERGE_THEME_KWS:
                    if any(k in _hay for k in kws):
                        return kws[0]
                return None

            _merge_theme_to_keep: dict[str, int] = {}  # theme -> keep index
            _merge_skip: set[int] = set()
            for _i, _q in enumerate(kb_questions):
                _th = _llm_merge_theme(_q)
                if not _th:
                    continue
                if _th not in _merge_theme_to_keep:
                    _merge_theme_to_keep[_th] = _i
                else:
                    _merge_skip.add(_i)
            if _merge_skip:
                _before_merge = len(kb_questions)
                _kept_ks = {kb_questions[i].get("question", "")[:40] for i in _merge_theme_to_keep.values()}
                kb_questions = [
                    q for i, q in enumerate(kb_questions) if i not in _merge_skip
                ]
                logger.info(
                    "[ClarifyAgent] N4 合并同主题细分 KB 查询: 原 {} 条 → 现 {} 条, 合并跳过 {} 条",
                    _before_merge, len(kb_questions), len(_merge_skip),
                )
        except Exception as _merge_e:
            logger.warning(
                "[ClarifyAgent] N4 同主题 KB 查询合并异常（安全降级不影响主流程）: {}",
                str(_merge_e)[:200],
            )

        # v2.8.1: KB 问题数量上限，防 token 累积超限
        # 系统庞大时 LLM 可能生成 8-10 个问题，每个注入 3×1500 字符到 LLM 校验，
        # 累积 token 可能超出小上下文模型限制。按优先级保留前 5 个。
        # v2.8.65: 上限从 5 调整为 8，容纳 LLM 语义问题转来的 KB 查询
        # v2.8.67: 排序第二级引入「来源插队权重」——来自统一分流池（LLM语义检测/引用未定义/
        # 优化专项 impact_analysis）的 query 永远比 completeness.baseline 纯规则层缺口优先
        # 原因：PRD 专项检测可能一次性产出 20+ 条 upper_bound_rule 基线缺口，若与引用未定义
        # 等真实强语义 query 同属 important 级别，稳定排序会把先入的 baseline 全排前面，
        # 导致 ref_undef（图片上传规则/历史版本策略等）被 MAX_KB_QUERIES 截断挤出——
        # 而 ref_undef 类问题恰恰是「KB 里最容易命中已归档历史规则」的高价值查询！
        MAX_KB_QUERIES = 8
        _kb_q_total = len(kb_questions)
        if _kb_q_total > MAX_KB_QUERIES:
            _priority_order = {"critical": 0, "important": 1, "optional": 2}
            # source_rank: 0 = 强语义（LLM / 统一池 ref_undef / impact_analysis），
            #             1 = 规则层 baseline 缺口
            # v2.8.67: _from_unified_candidate（规则层矛盾检测产出的 ref_undef 等）和
            #          _from_llm_semantic 同属 source_rank=0，避免被 baseline 挤出
            def _src_rank(q: dict) -> int:
                return 0 if (q.get("_from_llm_semantic") or q.get("_from_unified_candidate") or q.get("_kb_short_query")) else 1
            kb_questions.sort(key=lambda q: (
                _priority_order.get(q.get("priority", "important"), 1),
                _src_rank(q),
            ))
            kb_questions = kb_questions[:MAX_KB_QUERIES]
            logger.info("[ClarifyAgent] KB 问题上限截断: {} -> {}（按priority+来源插队权重保留高价值query）",
                        _kb_q_total, MAX_KB_QUERIES)

        _d3 = int((time.time() - _t3) * 1000)
        logger.info("[ClarifyAgent] 生成 KB 问题: count={}", len(kb_questions))

        # ⭐ 记录「KB 查询生成」阶段（无论是否为空，避免前端 0 条查询误以为漏做）
        if not kb_questions:
            # v2.8.8: 区分"规则层无缺口+LLM语义检测"和"规则层有缺口但都已覆盖"
            _rule_gaps = (
                len(completeness.get("critical_missing", []))
                + len(completeness.get("important_missing", []))
                + len(completeness.get("optimization_missing", []))
            )
            if _rule_gaps == 0:
                kb_access_log.append({
                    "action": "kb_query_gen",
                    "type": "kb_query_gen_empty",
                    "label": "KB 查询生成（语义检测通过）",
                    "query": "核心字段均存在，LLM 基于 SRR 全文进行语义遗漏检测后，未发现需要 KB 补全的维度。",
                    "query_detail": (
                        f"规则层完整性检测通过（关键缺失 0 项，重要缺失 0 项），"
                        f"LLM 语义遗漏检测通过（无输入校验/权限/边界/异常/数据约束等遗漏）"
                    ),
                    "result_count": 0,
                    "answered": False,
                    "duration_ms": _d3,
                    "status": "success",
                })
            else:
                kb_access_log.append({
                    "action": "kb_query_gen",
                    "type": "kb_query_gen_empty",
                    "label": "KB 查询生成（无缺口）",
                    "query": "根据完整性/歧义性/模式库检测，当前需求无可由 KB 自动补全的缺口维度，因此未生成 KB 查询。",
                    "query_detail": (
                        f"关键缺失 {len(completeness.get('critical_missing', []))} 项，"
                        f"重要缺失 {len(completeness.get('important_missing', []))} 项，"
                        f"优化建议缺失 {len(completeness.get('optimization_missing', []))} 项"
                    ),
                    "result_count": 0,
                    "answered": False,
                    "duration_ms": _d3,
                    "status": "success",
                })
        else:
            # v2.8.2: query_detail 用可读描述代替内部字段名 pattern_gap.xxx
            _gap_descs = []
            for q in kb_questions:
                _field = q.get('field', '')
                if _field.startswith('pattern_gap.'):
                    _gap_descs.append(q.get('missing_desc') or q.get('description') or _field.split('.', 1)[-1])
                else:
                    _gap_descs.append(q.get('missing_desc') or q.get('description') or _field)
            kb_access_log.append({
                "action": "kb_query_gen",
                "type": "kb_query_gen",
                "label": "KB 查询生成",
                "query": f"基于缺口分析生成 {len(kb_questions)} 条 KB 查询，用于检索同类需求补全 SRR。",
                "query_detail": (
                    f"查询维度：{'；'.join(_gap_descs[:10])}" if _gap_descs else "无具体维度"
                ),
                "result_count": len(kb_questions),
                "answered": False,
                "duration_ms": _d3,
                "status": "success",
            })

        # ④ 对每个问题查询 KB + PG，获取答案
        _t4 = time.time()
        # v2.8.55: 移除未使用的 global_similar 和 pg_context 参数
        # 这两个参数在 _query_kb_for_answers 内部从未被引用
        kb_answers: list[dict] = await self._query_kb_for_answers(
            srr, kb_questions,
        )
        _d4 = int((time.time() - _t4) * 1000)
        logger.info("[ClarifyAgent] KB 回答: answered={}, unanswered={}, duration={}ms",
                    sum(1 for a in kb_answers if a.get("answered")),
                    sum(1 for a in kb_answers if not a.get("answered")),
                    _d4)
        _answered = sum(1 for a in kb_answers if a.get("answered"))
        await _report(f"知识库检索完成（已答 {_answered}/{len(kb_answers)}），正在应用补全结果...", 60)
        # v2.8.59: 已彻底移除全局相似检索和 PG 关系增强的 kb_access_log 占位条目
        # 这两个步骤在 v2.8.55 已停止实际检索，占位条目仅造成前端展示混乱

        # ⑤ 应用 KB 答案 → 自动补全 SRR（含字段冲突处理，docs/01 §4.4.1）
        _t5 = time.time()
        augmented_srr, augmentations, field_conflicts = await self._apply_kb_answers(augmented_srr, kb_answers)
        _d5 = int((time.time() - _t5) * 1000)
        logger.info("[ClarifyAgent] KB 应用: augmentations={}, field_conflicts={}",
                    len(augmentations), len(field_conflicts))
        await _report(f"知识库补全应用完成（补全 {len(augmentations)} 项），正在精炼待澄清问题...", 66)

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
        # KB 查询已经通过规则兜底/语义转换的质量把控和 _verify_kb_answers 的答案审核，
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
                # v2.8.65: LLM 语义问题转来的 KB 未答查询，保留原始 category/context/options
                # 这类问题已有 LLM 生成的丰富 context 和 options，不应被模板覆盖
                _from_llm = qa.get("_from_llm_semantic", False)
                if _from_llm:
                    # 找回原始 LLM 语义问题（用于恢复 context/options/category）
                    # v2.8.67 修复：仅按 field 匹配会在「多个 ref_undef 同 field
                    # (requirement.ambiguity)」时把第 2+ 条的 context/options 错配到第 1 条。
                    # KB 查询的 question 文本是从原始问题逐字拷贝的（L856/L863/L2110），
                    # 因此追加 question 前40字符作为二次匹配键，精准定位回各自原问题。
                    _orig_llm_q = None
                    _qt_prefix = question_text[:40]
                    for lq in llm_kb_eligible_questions:
                        _olf = lq.get("field", "") or ""
                        _mapped = _olf if _olf.startswith("llm_semantic.") else f"llm_semantic.{_olf}"
                        if _mapped == field and (lq.get("question", "") or "")[:40] == _qt_prefix:
                            _orig_llm_q = lq
                            break
                    # 兜底：若 field+question 双键未命中（极端情况），退回仅 field 匹配，避免空指针
                    if _orig_llm_q is None:
                        for lq in llm_kb_eligible_questions:
                            _olf = lq.get("field", "") or ""
                            _mapped = _olf if _olf.startswith("llm_semantic.") else f"llm_semantic.{_olf}"
                            if _mapped == field:
                                _orig_llm_q = lq
                                break
                    questions.append({
                        "id": qid,
                        "field": field,
                        "priority": qa.get("priority", "important"),
                        "category": (_orig_llm_q or {}).get("category", "completeness"),
                        "question": question_text or q_data["question"],
                        "context": (_orig_llm_q or {}).get("context", "") or q_data["context"],
                        "default_answer": qa.get("missing_desc", ""),
                        "input_type": (_orig_llm_q or {}).get("input_type", q_data["input_type"]),
                        "options": (_orig_llm_q or {}).get("options", []) or q_data["options"],
                    })
                else:
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
            # v3.1.6: 选项精炼环节已移除——主问题生成（SYSTEM_PROMPT）每题直接带
            # options；KB 未答问题的 options 在步骤⑧构建处已从上游/模板注入。
            # 仅保留去重 + 必要性过滤 + options 修复三道收尾。
            questions = self._dedup_questions(questions, stage="after_refine")
            # v2.8.66: 过滤猜想上限类问题 + 修复options错乱（conflict/ambiguity真实问题不截数量）
            questions = self._filter_questions_by_necessity(questions)
            questions = self._validate_and_fix_options(questions)
            await _report(f"待澄清问题整理完成（{len(questions)} 个），正在汇总...", 70)
        else:
            questions = []
            await _report("无需 KB 补全的缺口，正在基于原始文本进行语义检测...", 70)

        # ⑧c v2.8.65: LLM 语义检测问题分流后的合并
        # v2.8.62 原逻辑：llm_semantic_questions 全部 extend 进最终列表（绕过 KB）
        # v2.8.65 新逻辑：
        #   - completeness/ambiguity 类已在步骤③转为 KB 查询，KB 能答的已补全，
        #     KB 未答的已在步骤⑧ 转为 kb_ 前缀人工问题 -> 此处不再合并
        #   - conflict 类（文档内部矛盾，KB 无法解决）-> 此处直接合并为人工问题
        # v2.8.67 新增：将 llm_direct_questions 中的「_direct_human_tail」（优化影响配套 2 个）
        #   单独拆出来最后追加，确保它们排在人工澄清列表的末尾（用户明确要求的固定位置）
        opt_tail_questions: list[dict] = []
        non_tail_direct: list[dict] = []
        for q in (llm_direct_questions or []):
            if q.get("_direct_human_tail"):
                opt_tail_questions.append(q)
            else:
                non_tail_direct.append(q)
        if non_tail_direct:
            questions.extend(non_tail_direct)
            logger.info(
                "[ClarifyAgent] 合并 LLM 语义检测 conflict 问题（直接人工，非末位）: {} 个",
                len(non_tail_direct),
            )
        # 合并后去重
        questions = self._dedup_questions(questions, stage="after_llm_semantic")

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

        # ⑨b 优化需求场景检测 + 逻辑矛盾检测
        # v2.8.67: 已在统一分流池（L739）中提前汇入 llm_kb_eligible_questions / llm_direct_questions。
        # 非 conflict 类问题（impact_analysis / ambiguity / completeness）已经先通过 KB 查询路径；
        # conflict 类问题（文档内部矛盾）已在 ⑧c（L1117）作为直接人工问题合并。
        # 因此此处不再重复生成，避免：
        #   ① 未查 KB 就打扰用户（违背「KB 回答不了的才进人工」原则）
        #   ② 重复问题输出（即使有去重也会占 MAX_KB_QUERIES 名额）
        # 原 ⑨b 调用和 contradiction 调用已删除，见 L739-L778 的统一分流池。

        # v2.8.66: 最终安全网：必要性过滤（删猜想类）+ options校验修复 + 去重
        questions = self._filter_questions_by_necessity(questions)
        questions = self._validate_and_fix_options(questions)
        questions = self._dedup_questions(questions, stage="final")

        # v2.8.72: 为引用未定义等规则类问题注入功能点归属，具体到功能点，
        # 防止不同功能点具有相同规则污染答案（仅归属首个相关功能点）。
        questions = self._attach_fp_scope(questions, augmented_srr)

        # v2.8.67: 优化影响配套问题末位追加——在 final 去重后单独追加，严格保证排末尾
        # 追加前按 field+id 双重去重：如果 questions 中已有同语义问题（可能来自KB路径或LLM层），
        # 就不再重复追加固定的优化配套问题
        if opt_tail_questions:
            _existing_keys = set()
            for q in questions:
                _existing_keys.add(f"field::{q.get('field', '')}")
                _existing_keys.add(f"id::{q.get('id', '')}")
                # 语义级去重：问题中包含"优化是否影响历史数据"也视为重复
                _qt = (q.get("question", "") or "")[:40]
                if "优化" in _qt and "历史数据" in _qt and "影响" in _qt:
                    _existing_keys.add("sem::opt_impact_dup")
            _tail_added = 0
            for q in opt_tail_questions:
                _fk = f"field::{q.get('field', '')}"
                _ik = f"id::{q.get('id', '')}"
                if _fk in _existing_keys or _ik in _existing_keys or "sem::opt_impact_dup" in _existing_keys:
                    logger.info(
                        "[ClarifyAgent] 优化配套末位问题去重跳过（已有同语义）: id={} field={}",
                        q.get("id"), q.get("field"),
                    )
                    continue
                questions.append(q)
                _tail_added += 1
            if _tail_added:
                logger.info(
                    "[ClarifyAgent] 末位追加优化影响配套问题: {} 个（排人工澄清末尾）", _tail_added,
                )

        # v2.8.54: 清理临时字段 _llm_is_optimization，避免持久化到数据库 SRR
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
                "kb_unavailable": kb_unavailable,
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
                "llm_kb_query_gen_ms": _d3,
                "kb_qa_ms": _d4,
                "kb_apply_ms": _d5,
                # v3.1.7 fix: 选项精炼环节已在 v3.1.6 移除（含 _t_refine/_d_refine 计时），
                # 此处残留引用会在含 KB 未答问题的轮次直接 NameError——保留 key 兼容前端，恒为 0
                "llm_question_refine_ms": 0,
            },
        }

        logger.info("[ClarifyAgent] ◀ 澄清完成 ▶ 下一节点 QualityGate: status={}, "
                    "overall_score={:.3f}, pass_threshold={}, questions={}, cold_start={}",
                    status, quality_score, threshold, len(questions), cold_start)
        return result

    # ========================================================================
    # 答案覆盖评估（v3.0.1 多轮收敛重构）
    # 核心思路：提交答案后不再从零做完整缺口检测，而是「逐条评估上一轮问题
    # 的答案是否已覆盖该问题」。覆盖 → 收敛；未覆盖/模糊 → 追问；用户跳过
    # （未回答）→ 视为不关注，不再追问。这样正常 1 轮正确回答即收敛。
    # ========================================================================
    async def eval_answer_coverage(
        self,
        srr: dict,
        clarify_history: list | None = None,
        progress_callback=None,
    ) -> dict:
        """评估上一轮澄清问题的答案覆盖情况，返回需追问的问题列表。

        Args:
            srr: 重解析后的最终 SRR（含答案合并后的内容）
            clarify_history: 澄清历史（提交答案时传入），取最后一条的 questions+answers
            progress_callback: 可选进度回调

        Returns:
            dict: 与 clarify() 兼容的结构，其中 questions 仅包含「未覆盖/模糊」的追问，
                  已覆盖或已跳过的问题不再出现。
        """
        req_id = (srr.get("requirement") or {}).get("id", "")
        kb_status = {"kb_empty": False, "product_first_requirement": False, "kb_unavailable": False}

        # 1. 取上一条澄清记录（questions + answers）
        prev_questions: list[dict] = []
        prev_answers: dict = {}
        hist = clarify_history if clarify_history is not None else (srr.get("_clarify_history") or [])
        if isinstance(hist, list) and hist:
            last = hist[-1]
            if isinstance(last, dict):
                prev_questions = last.get("questions") or []
                _ans = last.get("answers") or {}
                prev_answers = {str(k): v for k, v in _ans.items()} if isinstance(_ans, dict) else {}

        if not prev_questions:
            logger.info("[ClarifyAgent] eval_answer_coverage: 无上一轮问题，直接收敛")
            return {
                "status": "passed",
                "questions": [],
                "auto_augmentations": [],
                "quality_report": {"overall_score": 1.0, "pass_threshold": self.PASS_THRESHOLD},
                "kb_status": kb_status,
                "kb_access_log": [],
                "field_conflicts": [],
            }

        # 2. 分离：跳过的（不关注） vs 已回答的（需评估覆盖）
        unanswered_or_skipped: list[dict] = []   # 答案为空 → 不关注，不再追问
        answered: list[dict] = []                # 有答案 → 需 LLM 评估是否覆盖
        for q in prev_questions:
            if not isinstance(q, dict):
                continue
            qid = str(q.get("id", "") or "")
            ans = prev_answers.get(qid)
            if isinstance(ans, list):
                ans = ",".join(str(x) for x in ans if x)
            ans = str(ans or "").strip()
            if not ans:
                unanswered_or_skipped.append(q)
            else:
                answered.append({"question": q, "answer": ans})

        # 3. 对已回答的问题，用 LLM 评估是否已覆盖问题核心
        followup: list[dict] = []
        if answered:
            items = []
            for it in answered:
                qtext = (it["question"].get("question", "") or "")[:200]
                items.append({
                    "id": it["question"].get("id", ""),
                    "question": qtext,
                    "answer": it["answer"][:300],
                })
            try:
                prompt = prompts.ANSWER_COVERAGE_EVAL_PROMPT
                result = await self._llm.call_json(
                    prompt=prompt.format(
                        raw_text=(srr.get("_raw_text", "") or "")[:12000],
                        items=json.dumps(items, ensure_ascii=False),
                    ),
                    system_prompt=prompt_manager.get("clarify_agent", "CLARIFY_COVERAGE_EVAL_SYSTEM_PROMPT"),
                    schema_hint={"items": list},
                    temperature=0.1,
                    agent_name="ClarifyAgent.CoverageEval",
                )
                evals = result.get("items", []) if isinstance(result, dict) else []
                eval_by_id = {}
                for e in evals:
                    if isinstance(e, dict):
                        eval_by_id[str(e.get("id", ""))] = e
                for it in answered:
                    qid = str(it["question"].get("id", "") or "")
                    cov = eval_by_id.get(qid) or {}
                    decision = str(cov.get("coverage", "") or "").lower()
                    if decision in ("covered", "yes", "y"):
                        logger.info("[ClarifyAgent] 答案已覆盖，收敛: id={} question={}",
                                    qid, (it["question"].get("question", "") or "")[:40])
                        continue
                    # 未覆盖 / 模糊 / 不完整 → 追问（保留原问题，标记追问）
                    qi = dict(it["question"])
                    qi["_followup"] = True
                    qi["_followup_reason"] = str(cov.get("reason", "") or "")[:200]
                    followup.append(qi)
                logger.info("[ClarifyAgent] eval_answer_coverage: answered={}, followup={}, skipped={}",
                            len(answered), len(followup), len(unanswered_or_skipped))
            except Exception as e:
                logger.warning("[ClarifyAgent] 答案覆盖评估失败，保守处理：全部视为未覆盖追问: {}", str(e)[:200])
                followup = [dict(it["question"]) for it in answered]

        # 4. 收敛判定
        if followup:
            status = "needs_clarification"
        else:
            status = "passed"
        logger.info("[ClarifyAgent] ◀ eval_answer_coverage ▶ req_id={}, status={}, followup={}",
                    req_id, status, len(followup))
        return {
            "status": status,
            "questions": followup,
            "auto_augmentations": [],
            "quality_report": {"overall_score": 1.0 if not followup else 0.8,
                               "pass_threshold": self.PASS_THRESHOLD},
            "kb_status": kb_status,
            "kb_access_log": [],
            "field_conflicts": [],
        }

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

        # v3.1.6: 优化需求判断已移除（COMPLETENESS_CHECKLIST 的 optimization 级别也已注释停用），
        # 历史数据影响类问题由主问题生成的语义检测按原文内容自然覆盖
        for level, fields in prompts.COMPLETENESS_CHECKLIST.items():
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

    def _detect_cross_doc_conflicts(self, srr: dict) -> list[dict]:
        """跨文档矛盾预检测（v2.8.64 新增，规则层）。

        当 _raw_text 中包含多个「【文档：XXX】」标记时（多文件上传场景），
        先在规则层做轻量存在性检测（不做语义判断，由 LLM 最终负责），
        把「存在多文档，可能有矛盾」作为线索注入 ambiguities，让 LLM 在
        _generate_questions 中重点排查跨文档矛盾。

        返回值: 与 ambiguities 结构一致的 list[dict]，每个元素含
            {type, field, description, evidence, _suggested_question, _suggested_options}
        """
        raw_text = srr.get("_raw_text", "") or ""
        results: list[dict] = []

        # 1. 检测是否多文档（【文档：XXX】出现 >=2 次）
        import re
        doc_markers = re.findall(r"【文档：([^】]+)】", raw_text)
        if len(doc_markers) < 2:
            return results  # 单文档，无需跨文档矛盾检测

        # 记录文档列表作为 evidence
        doc_list = "、".join(doc_markers)

        # 2. 扫描常见的矛盾候选对（规则层只做候选提示，不做判定）
        #    LLM 负责在 _generate_questions 中结合语义判断是否真矛盾
        conflict_hints = [
            # 2a. 同名字段 / 同名规则 出现多次且值可能不同
            (r"(用户角色|权限|功能范围|支持的?[格式大小]|并发|上限|阈值|触发条件|降级策略|默认值|超时时间|校验规则|必填|可选)",
             "same_keyword_diff_value",
             "同一名词在不同文档中出现，请检查是否定义不一致"),
        ]
        # 为每个「同名词出现在不同文档」的候选生成 1 条 conflict 候选问题
        seen_keywords: set[str] = set()
        for pattern, ctype, desc in conflict_hints:
            # 找出每个匹配点所在的文档片段（粗粒度：按【文档：XXX】切片）
            doc_slices = re.split(r"(?=【文档：[^】]+】)", raw_text)
            kw_per_doc: dict[str, set[str]] = {}
            for sl in doc_slices:
                m_doc = re.search(r"【文档：([^】]+)】", sl)
                if not m_doc:
                    continue
                doc_name = m_doc.group(1)
                kws = re.findall(pattern, sl)
                if kws:
                    kw_per_doc.setdefault(doc_name, set()).update(kws)
            # 统计某关键词在 >=2 个文档中同时出现
            kw_docs: dict[str, list[str]] = {}
            for doc_name, kws in kw_per_doc.items():
                for kw in kws:
                    kw_docs.setdefault(kw, []).append(doc_name)
            for kw, docs in kw_docs.items():
                if len(docs) >= 2 and kw not in seen_keywords:
                    seen_keywords.add(kw)
                    results.append({
                        "type": "cross_doc_keyword_candidate",
                        "field": f"cross_doc.keyword.{kw}",
                        "description": f"关键词「{kw}」同时出现在文档 {', '.join(docs)} 中，需人工或 LLM 验证是否定义一致",
                        "evidence": f"涉及文档: {', '.join(docs)}; 关键词: {kw}",
                        "_suggested_question": (
                            f"不同文档中关于「{kw}」的描述是否一致？（涉及文档：{', '.join(docs)}）"
                            f"若存在不一致，请指出差异并以哪个文档的定义为准？"
                        ),
                        "_suggested_options": [
                            f"以 {docs[0]} 为准",
                            f"以 {docs[-1]} 为准",
                            "两者需合并说明",
                            "不存在不一致",
                        ],
                    })
                    if len(results) >= 15:
                        break  # 候选太多时截断，LLM 自己继续找
            if len(results) >= 15:
                break

        # 3. 保底：无论是否检测到候选，多文档场景都追加 1 条「请核对是否有跨文档矛盾」的提醒
        #    （作为 ambiguity 而非 conflict，避免误报）
        if results:
            results.append({
                "type": "cross_doc_reminder",
                "field": "cross_doc.global_check",
                "description": f"本次上传共 {len(doc_markers)} 个文档（{doc_list}），请全局核对跨文档是否存在定义冲突、矛盾或重复",
                "evidence": f"文档列表: {doc_list}",
                "_suggested_question": (
                    f"本次需求涉及 {len(doc_markers)} 个文档：{doc_list}。"
                    "请确认这些文档之间是否存在定义冲突（如同一功能在不同文档描述不同）、"
                    "遗漏或重复？若有请具体说明。"
                ),
                "_suggested_options": [
                    "不存在跨文档冲突",
                    "存在冲突，将在后续补充说明",
                    "存在遗漏/重复，将在后续补充说明",
                ],
            })
        return results

    def _detect_ambiguity(self, srr: dict) -> list[dict]:
        """歧义检测的规则层部分。

        v2.9: 纯关键词匹配（AMBIGUITY_PATTERNS）已移除，避免机械误报。
        v2.8.64: 新增多文档场景下的「跨文档矛盾候选」预检测（只做存在性
            和候选提示，不做语义判定，最终仍由 LLM 负责）。结果直接返回
            供主流程注入 ambiguity，并由 build_user_prompt 透传给 LLM
            作为重点检测线索。
        """
        cross = self._detect_cross_doc_conflicts(srr)
        return cross

    # ========================================================================
    # KB 检索
    # ========================================================================

    def _safe_is_empty(self) -> tuple[bool, bool]:
        """v2.8.55 Bug C-2: 判断向量库是否为空。

        Returns:
            (is_empty, is_error): 当 KB 不可用时返回 (False, True)，
            避免异常被误判为真空导致冷启动误判。
        """
        try:
            return vector_store.is_empty(), False
        except Exception as e:
            logger.warning("[ClarifyAgent] KB is_empty 调用异常（标记为不可用）: {}", str(e)[:150])
            return False, True

    def _safe_is_product_first(self, product_id: str) -> tuple[bool, bool]:
        """v2.8.55 Bug C-2: 判断是否为产品首个需求。

        Returns:
            (is_first, is_error): 当 KB 不可用时返回 (False, True)，
            避免异常被误判为首个需求导致冷启动误判。
        """
        try:
            return vector_store.is_product_first_requirement(product_id), False
        except Exception as e:
            logger.warning("[ClarifyAgent] KB is_product_first 调用异常（标记为不可用）: {}", str(e)[:150])
            return False, True

    # v2.8.59: 移除死代码 _search_similar_requirements 方法
    # v2.8.55 已移除主流程中的调用，该方法无任何引用，保留仅造成维护困惑
    # 真实 KB 检索由 _query_kb_for_answers 内部调用 hybrid_searcher 完成

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

    def _fallback_gap_queries(
        self, completeness: dict, is_incremental: bool, covered_fields: set[str],
        srr: dict | None = None,
    ) -> list[dict]:
        """规则兜底：未被主问题生成覆盖的规则缺口 → KB 查询（v3.1.6）。

        v2.10-v3.1.5 曾是独立的 KB 查询生成环节（先 LLM 后规则），v3.1.6 起合并到
        主问题生成（SYSTEM_PROMPT 每题直接给出 kb_query），本方法仅作为兜底保留：
        LLM 问题未覆盖的缺口（按 field 匹配 covered_fields）仍须查 KB，
        防止规则层检测到的缺口静默丢失。

        映射逻辑：每个缺口 1 条查询——优先用缺口自带的 _suggested_question，
        否则用缺口描述（本身带业务语义，作为 hybrid 向量+BM25 查询足够有效）。

        Args:
            completeness: _check_completeness 的输出
            is_incremental: 是否增量澄清（含 baseline_gaps）
            covered_fields: 已被 LLM 问题覆盖的 field 集合（这些缺口不再生成）
            srr: SRR 字典（增量澄清时读取 _baseline_gaps，可为 None）

        Returns:
            list[dict]: KB 查询列表 [{field, missing_desc, priority, question, _is_baseline}]
        """
        raw_queries: list[dict] = []

        def _add_gap(gap: dict, default_priority: str) -> None:
            field = str(gap.get("field", "") or "").strip()
            if not field:
                return
            # 优先用缺口自带的建议问题（规则层 PRD/歧义检测已生成），否则用描述
            question = (
                gap.get("_suggested_question")
                or gap.get("question")
                or gap.get("description")
                or ""
            ).strip()
            if not question:
                return
            _raw_field = field
            _fallback_desc = (
                _raw_field.split(".", 1)[-1]
                if _raw_field.startswith("pattern_gap.")
                else _raw_field
            )
            raw_queries.append({
                "field": _raw_field,
                "missing_desc": gap.get("description") or _fallback_desc,
                "priority": gap.get("priority") or default_priority,
                "question": question,
                "_is_baseline": False,
            })

        def _not_covered(gap: dict) -> bool:
            """LLM 问题已覆盖的缺口（field 命中）不再生成兜底查询。"""
            f = str(gap.get("field", "") or "").strip()
            return not f or f not in covered_fields

        for gap in completeness.get("critical_missing", []) or []:
            if _not_covered(gap):
                _add_gap(gap, "critical")
        for gap in completeness.get("important_missing", []) or []:
            if _not_covered(gap):
                _add_gap(gap, "important")
        for gap in completeness.get("optimization_missing", []) or []:
            if _not_covered(gap):
                _add_gap(gap, "important")

        # 增量澄清：baseline 缺口同样转查询
        if is_incremental:
            baseline = (srr or {}).get("_baseline_gaps")
            if isinstance(baseline, list):
                for g in baseline:
                    if isinstance(g, dict):
                        _add_gap(g, "important")
                    elif isinstance(g, str) and g.strip():
                        raw_queries.append({
                            "field": f"baseline.{g.strip()}",
                            "missing_desc": g.strip(),
                            "priority": "important",
                            "question": f"【{g.strip()}】在历史同类需求中是如何实现的？",
                            "_is_baseline": True,
                        })

        # field+question 双重去重（与旧版一致）
        seen_query_keys: set[str] = set()
        validated: list[dict] = []
        for q in raw_queries:
            dedup_key = f"{q['field']}::{q['question'][:40]}"
            if dedup_key in seen_query_keys:
                continue
            seen_query_keys.add(dedup_key)
            validated.append(q)

        # 必要性过滤（猜想型 upper_bound_rule 不进入 KB 检索）
        validated = ClarifyAgent._filter_questions_by_necessity(validated)
        if validated:
            logger.info("[ClarifyAgent] 规则生成 KB 查询: {} 条", len(validated))
        return validated


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
    ) -> list[dict]:
        """对每个问题查询知识库并尝试获取答案。

        v2.8.52: 不再接收外部 session，内部 DB 查询使用短生命周期 session。
        v2.8.55: 移除未使用的 all_similar_reqs 和 pg_context 参数。
               这两个参数从未在方法内被引用，删除以避免误导。

        流程:
        1. 用问题文本搜索向量库 → 相似需求（hybrid_search 含时间衰减排序）
        2. 从搜索结果中提取答案
        3. LLM 校验答案相关性
        4. 记录 Q&A 对

        Args:
            srr: 当前需求 SRR
            questions: _formulate_kb_questions 生成的问题列表

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
            # v2.8.52: 使用内部短生命周期 session
            # v2.8.53: 增加 doc_type="requirement" 过滤，避免 FP 向量混入补全上下文
            # （FP 向量的 srr_json 是单功能点 SRR，结构不同，混入会导致补全粒度错乱）
            # v2.8.67: 优先使用 _kb_short_query（ref_undef 类的短主题关键词），
            #  否则回退到 question 全文。长 question 含大量上下文噪声会导致
            #  BM25 分词后核心词被稀释，明明有历史规则却零命中。
            try:
                from app.db.hybrid_search import hybrid_searcher
                from app.db.database import async_session as _as
                current_modules = srr.get("requirement", {}).get("modules", []) or []
                async with _as() as tmp_session:
                    _kb_q = q.get("_kb_short_query") or q["question"]
                    _kb_q_source = "short" if q.get("_kb_short_query") else "full_question"
                    if _kb_q_source == "short":
                        logger.debug(
                            "[ClarifyAgent] KB 查询使用短 query: source={} short={!r} (原长={})",
                            _kb_q_source, _kb_q, len(q["question"]),
                        )
                    matched = await hybrid_searcher.search_requirements(
                        query=_kb_q, top_k=5,
                        product_id=srr.get("requirement", {}).get("product_id", ""),
                        modules=current_modules,
                        status="archived", session=tmp_session,
                        doc_type="requirement",
                    )
            except Exception as e:
                # 降级策略：KB 不可用 → 静默跳过 → 继续生成澄清问题 → 人工澄清兜底
                # 不影响生成流程，不注入诊断信息到 Prompt
                logger.warning("[ClarifyAgent] KB 查询失败: q={!r} error={}", q.get("_kb_short_query") or q.get("question", "")[:80], str(e)[:200])
                matched = []

            if matched:
                sims = [round(m.get("similarity", 0), 3) for m in matched]
                logger.info("[ClarifyAgent] KB 命中: q={!r} count={} sims={}", q.get("question", "")[:60], len(matched), sims)

            # v2.8.55: 移除应用层 go_live_date 覆盖排序
            # 原因：该排序破坏了 hybrid_search 的 RRF + CrossEncoder 相关性结果
            #       现由 hybrid_search._apply_time_decay 在 RRF 分数上应用时间衰减
            #       既保留相关性主导，又让较新版本在相同相关性下优先

            # P5 修复：按 req_id 去重，避免同一需求/FP 重复展示
            # 主需求向量以 req_id 标识，FP 向量以 FP-{fp_id} 标识。
            # v2.8.55: 同一 req_id 多版本去重时，优先保留 go_live_date 最新的版本
            #        （而非相似度最高），确保迭代多次后取最新版本
            seen_req_ids: dict[str, dict] = {}
            for m in matched:
                meta = m.get("metadata", {}) or {}
                dedup_key = meta.get("req_id", m.get("id", ""))
                if not dedup_key:
                    continue
                # v2.8.55: 版本感知去重——优先保留 go_live_date 最新的版本
                existing = seen_req_ids.get(dedup_key)
                if existing is None:
                    seen_req_ids[dedup_key] = m
                else:
                    existing_date = (existing.get("metadata", {}) or {}).get("go_live_date", "")
                    current_date = meta.get("go_live_date", "")
                    if current_date > existing_date:
                        seen_req_ids[dedup_key] = m
            matched = list(seen_req_ids.values())

            # 从匹配需求中提取答案
            answer_text = ""
            sources = []
            for m in matched:
                # 问题→文档语义匹配阈值 0.40（v2.8.1 从 0.40 提升到 0.50 后过度误杀）。
                # bge-large-zh 归一化向量下，具体问题与整篇 SRR 文本的余弦相似度
                # 天然偏低（典型 0.45-0.65），0.50 会丢弃大量有效历史匹配，导致澄清
                # 阶段 KB「未找到」而独立问答 /knowledge-qa/ask（无此阈值）却能命中。
                # 回退到 0.40，由后续 _verify_kb_answers 的 LLM 校验作为质量兜底，
                # 拒绝不相关答案，避免误匹配污染。
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
                elif doc:
                    # v2.8.70 KB 命中增强：检索命中但按 field 路径提取不到值（如
                    # ref_undef 的 field="llm_semantic.requirement.ambiguity" 是标记字段，
                    # 历史 SRR 无此路径）时，仍保留来源交给 _verify_kb_answers 的 LLM
                    # 从 document_full（整篇历史 SRR）提取「针对问题的具体答案」。
                    # 根因：此前命中后 sources 为空 → answered=False → 被误判为
                    # 「知识库未命中」转人工，即使历史版本确实含该主题规则。
                    # v2.8.71: 展示用历史需求的干净概述（title+summary），
                    # 而非嵌入文本（doc 含 [happy_path] 等结构化噪音）。
                    display = ""
                    if src_srr:
                        _req = src_srr.get("requirement", {}) or {}
                        display = (_req.get("summary", "") or "").strip()
                    display = (display[:300] if display else doc[:300])
                    answer_text += f"• {req_id}{meta_suffix}：{display}\n"
                    sources.append({
                        "id": req_id,
                        "similarity": round(m.get("similarity", 0), 3),
                        "field": q["field"],
                        "value": display or doc[:1000],
                        # 整篇历史 SRR 供 LLM 校验提取针对性片段
                        "document_full": json.dumps(src_srr, ensure_ascii=False)[:3000] if src_srr else doc[:3000],
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
                # v2.8.65: 透传 LLM 语义问题来源标记，供步骤⑧ 生成问题时保留原始 category/options
                "_from_llm_semantic": q.get("_from_llm_semantic", False),
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
        # v2.8.72: LLM 校验结果可能不回显 index（实测常缺失），
        # 用 question 文本建立映射，作为 index 缺失/错位时的定位兜底。
        question_to_idx: dict[str, int] = {}
        for _ci in candidate_indices:
            _q = str((answers[_ci].get("question") or "") or "").strip()
            if _q:
                question_to_idx[_q] = _ci

        # 构建校验请求：每个候选答案的查询 + 检索到的文档内容
        req_title = (srr.get("requirement", {}) or {}).get("title", "当前需求")
        req_summary = (srr.get("requirement", {}) or {}).get("summary", "")
        verification_items: list[dict] = []
        for idx in candidate_indices:
            a = answers[idx]
            docs: list[dict] = []  # v2.8.55: 改为 dict 列表，携带版本信息
            for s in a.get("sources", [])[:3]:
                # 优先用整篇 SRR 文档上下文（document_full），让 LLM 从中提取
                # 「针对问题的具体片段」；回退到字段值 value（机械整字段提取）
                doc_full = s.get("document_full")
                if doc_full:
                    doc_content = doc_full[:1500]
                else:
                    v = s.get("value")
                    if isinstance(v, (dict, list)):
                        doc_content = json.dumps(v, ensure_ascii=False)[:600]
                    elif v:
                        doc_content = str(v)[:600]
                    else:
                        doc_content = ""

                # v2.8.55: 携带版本信息，让 LLM 感知文档新旧
                go_live = s.get("go_live_date", "")
                proj_ver = s.get("project_version", "")
                docs.append({
                    "content": doc_content,
                    "go_live_date": go_live or "",
                    "project_version": proj_ver or "",
                    "source_id": s.get("id", ""),
                    "similarity": s.get("similarity", 0),
                })
            verification_items.append({
                "index": idx,
                "question": a["question"],
                "documents": docs,
            })

        # v2.8.1: 批次大小限制，防 LLM token 超限
        # 每批最多 5 个问题 × 3 来源 × 1500 字符 ≈ 5,600 tokens，加 prompt 约 7K，安全。
        KB_VERIFY_BATCH = 5
        if len(verification_items) > KB_VERIFY_BATCH:
            logger.info("[ClarifyAgent] KB 校验分批: total={}, batch={}", len(verification_items), KB_VERIFY_BATCH)

        # 分批校验，合并结果
        for batch_start in range(0, len(verification_items), KB_VERIFY_BATCH):
            batch_items = verification_items[batch_start:batch_start + KB_VERIFY_BATCH]
            batch_num = batch_start // KB_VERIFY_BATCH + 1
            user_prompt = (
                f"【当期需求】\n"
                f"标题: {req_title}\n"
                f"摘要: {req_summary}\n"
                f"\n"
                f"【待审核的查询与检索结果】（第 {batch_num} 批）\n"
                f"每个文档携带 go_live_date（上线时间）和 project_version（项目版本）。\n"
                f"当多个文档内容冲突时，**优先采纳 go_live_date 最新的文档**（迭代多次后取最新版本）。\n"
                f"如果文档内容与查询不相关或不在当期需求范围内，判定为 relevant=false。\n"
                f"{json.dumps(batch_items, ensure_ascii=False, indent=2)}\n"
                f"\n"
                f"请逐条审核：\n"
                f"1. 检索到的文档内容是否真正与查询相关、且在当期需求范围内\n"
                f"2. 若多个文档内容冲突，优先采纳 go_live_date 最新的版本\n"
                f"3. 只要判定为相关（relevant=true），必须给出一个非空的 extracted_answer\n"
                f"4. extracted_answer 是**综合所有相关文档后精炼出的最终答案**："
                f"直接回答查询所问的问题（如'自动保存上限20个，手动标记不设上限，超过最旧自动降级'），"
                f"不要罗列各文档的标题/概述原文，不要逐条粘贴\n"
                f"5. extracted_answer 必须压缩为**单一精炼答案**：一句话或一段话，"
                f"禁止输出多条以 • / - / * 开头的列表项，禁止出现多个'需求标题'或'REQ-'编号\n"
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
                    # v2.8.72: 优先用 index 定位；LLM 常不回显 index 或回显错位，
                    # 用 question 文本匹配兜底（精确 → 子串包含）。
                    idx = v.get("index")
                    if (
                        not isinstance(idx, int)
                        or idx < 0
                        or idx >= len(answers)
                        or idx not in candidate_indices
                    ):
                        q_text = str(v.get("question", "") or "").strip()
                        idx = question_to_idx.get(q_text)
                        if idx is None:
                            for _qk, _qidx in question_to_idx.items():
                                if q_text and (q_text in _qk or _qk in q_text):
                                    idx = _qidx
                                    break
                    if idx is None:
                        continue
                    if v.get("relevant"):
                        # 基线补全：answer 已是精准维度摘要，不再用整篇文档覆盖（修复「全量回答」）
                        if answers[idx].get("_is_baseline"):
                            pass
                        else:
                            # LLM 确认相关：用 LLM 提取的答案替换机械答案
                            extracted = v.get("extracted_answer", "")
                            if not extracted:
                                # v2.8.72: LLM 判定相关但无法给出精炼答案（信息不充分）时，
                                # **不使用降低回答质量的兜底**（如罗列原文或取单条概述），
                                # 而是转人工澄清，符合「LLM未返回精炼结果时不兜底」规则。
                                logger.info(
                                    "[ClarifyAgent] KB 答案信息不充分，转人工澄清: query={} reason={}",
                                    answers[idx].get("question", "")[:80],
                                    v.get("reason", "")[:100],
                                )
                                answers[idx]["answered"] = False
                                answers[idx]["answer"] = "知识库检索信息不充分，无法给出精炼答案，需人工澄清"
                                answers[idx]["sources"] = []
                                answers[idx]["auto_filled"] = False
                            else:
                                # v2.8.72: LLM 返回的 extracted_answer 若仍为多文档罗列
                                #（含多条 • / REQ- / 需求标题），收敛为单一精炼答案
                                if self._is_doc_listing(extracted):
                                    extracted = self._kb_clean_summary(answers[idx])
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
                    "[ClarifyAgent] KB 答案校验 LLM 调用失败（第 {} 批），转人工澄清: {}",
                    batch_num, str(e)[:200],
                )
                # v2.8.72: LLM 校验失败时不再原样保留多文档罗列，也不给低质量兜底，
                # 而是将每个已答候选转人工澄清，避免把原文全部返回给用户。
                for _ci in candidate_indices:
                    if _ci >= len(answers):
                        continue
                    if answers[_ci].get("_is_baseline"):
                        continue
                    if answers[_ci].get("answered"):
                        answers[_ci]["answered"] = False
                        answers[_ci]["answer"] = "知识库答案校验失败，需人工澄清"
                        answers[_ci]["sources"] = []
                        answers[_ci]["auto_filled"] = False

        return answers

    @staticmethod
    def _is_doc_listing(text: str) -> bool:
        """判断字符串是否为多文档罗列答案（v2.8.72）。

        当 LLM 校验异常或未收敛时，答案会残留 _query_kb_for_answers 构建的
        多文档罗列（多条 "• REQ-..." / "需求标题" / 多个 bullet 行）。
        检测到即触发收敛为单一精炼答案，避免把原文全部返回给用户。
        """
        if not text or not isinstance(text, str):
            return False
        # 多个 bullet 行（列表）
        bullets = [ln for ln in text.splitlines() if ln.strip().startswith(("•", "-", "*"))]
        if len(bullets) >= 2:
            return True
        # 含多个 REQ- 编号 / 需求标题 标记
        for marker in ("需求标题", "REQ-"):
            if text.count(marker) >= 2:
                return True
        return False

    @staticmethod
    def _kb_clean_summary(a: dict) -> str:
        """从 KB 命中的历史 SRR 提取干净的精炼答案（v2.8.71 强化）。

        当 LLM 校验未提取出精炼答案时，回退用历史需求合并出**单一精炼答案**：
        - 只保留相似度最高的 1 条来源（sources 已按相似度降序），内容聚焦；
        - 优先用该来源的 summary（已含具体规则，如"自动保存上限20个"）；
        - 不再罗列多条 title+summary，避免把原文全部返回给用户。
        """
        parts: list[str] = []
        for s in (a.get("sources") or [])[:1]:
            doc_full = s.get("document_full", "")
            if not doc_full:
                continue
            try:
                src = json.loads(doc_full) if isinstance(doc_full, str) else (doc_full or {})
            except Exception:
                continue
            req = src.get("requirement", {}) or {}
            title = (req.get("title", "") or "").strip()
            summary = (req.get("summary", "") or "").strip()
            if not (title or summary):
                continue
            ver = s.get("project_version", "") or ""
            go_live = s.get("go_live_date", "") or ""
            tag = f"v{ver}" if ver else ""
            dtag = f"上线:{str(go_live)[:10]}" if go_live else ""
            meta = ", ".join(t for t in [tag, dtag] if t)
            suffix = f"（{meta}）" if meta else ""
            if summary:
                parts.append(f"{summary[:300]}{suffix}")
        return "\n".join(parts)

    @staticmethod
    def _attach_fp_scope(questions: list[dict], srr: dict) -> list[dict]:
        """为引用未定义等规则类澄清问题注入功能点归属（v2.8.72）。

        用户诉求：澄清问题需具体到功能点，防止不同功能点具有相同规则污染答案。
        归属策略（仅归属首个相关功能点）：
        - 用问题中的主题关键词与 fp_summary 功能点名称匹配，取首个相关功能点；
        - 无匹配则取第一个功能点；
        - 无功能点则标注「全局规则」。
        同时修正 context 中「已解析出 N 个功能点」为真实数量与名称，避免 LLM 幻觉数字误导用户。
        """
        try:
            fp_summary = srr.get("fp_summary") or []
            fp_names = [f.get("name", "") for f in fp_summary if f.get("name")]
            fp_count = len(fp_names)
            if not fp_names:
                return questions
            fp_list_note = "、".join(fp_names)
            for q in questions:
                if not isinstance(q, dict):
                    continue
                # ① 对所有问题：修正 context 中「已解析出 N 个功能点」数量幻觉，
                #    统一替换为真实数量与名称，避免 LLM 幻觉数字误导用户。
                #    仅当 context 尚未出现「个功能点（」功能点列表时替换，避免重复括号
                #   （_build_field_question 模板已含「（功能点列表）」时不再重复插入）。
                _ctx = q.get("context", "") or ""
                if (
                    "个功能点" in _ctx
                    and "个功能点（" not in _ctx
                    and "个功能点(" not in _ctx
                ):
                    revised = re.sub(
                        r"已解析出\s*\d+\s*个功能点",
                        f"已解析出 {fp_count} 个功能点（{fp_list_note}）",
                        _ctx,
                    )
                    if revised != _ctx:
                        q["context"] = revised
                # ② 仅对引用未定义 / 沿用现有规则 类规则问题注入功能点归属
                qt = q.get("question", "") or ""
                is_rule_q = (
                    "引用未定义" in qt
                    or ("规则" in qt and ("未给出" in qt or "未定义" in qt or "沿用" in qt))
                )
                if not is_rule_q:
                    continue
                if "（功能点：" in qt:  # 已注入过，跳过
                    continue
                # 提取主题词（「XXXX」内），用于与功能点名匹配
                theme = ""
                _m = re.search(r"「([^」]+)」", qt)
                if _m:
                    theme = _m.group(1).strip()
                # 仅归属首个相关功能点：主题词命中功能点名 → 取该功能点；否则取第一个
                chosen = fp_names[0]
                for fn in fp_names:
                    if theme and theme in fn:
                        chosen = fn
                        break
                # 注入功能点归属到 question
                q["question"] = qt.replace(
                    "相关规则未给出具体内容",
                    f"规则（功能点：{chosen}）未给出具体内容",
                    1,
                )
        except Exception as e:
            logger.warning("[ClarifyAgent] 注入功能点归属失败（跳过，不影响问题生成）: {}", str(e)[:120])
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
        # v3.1.5: 砍掉 FIELD_CONFLICT_RESOLVER LLM 裁决轮子（用户要求）。
        # 字段值冲突的「检测」已由主问题生成（SYSTEM_PROMPT 第4维度矛盾检测）覆盖，
        # 冲突不再由 LLM 自动选边，统一转人工确认——避免 LLM 误裁导致用户输入被静默覆盖。
        existing_priority = FIELD_PRIORITY.get(existing_source, 0)
        if existing_priority <= FIELD_PRIORITY["HUMAN_ANSWERED"]:
            # 用户输入或人工回答 vs KB 值冲突 → 直接转 pending_human
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

            # v2.8.70 KB 命中增强：标记字段（LLM 语义 / 引用未定义）的 KB 答案
            # 不写回具体业务字段——其 field（如 "llm_semantic.requirement.ambiguity"）
            # 是检测标记而非真实 SRR 路径，写回会污染 SRR。达到此处的答案已通过
            # _verify_kb_answers 的 LLM 相关性校验（answered=True），因此视为
            # 「已由 KB 回答」→ 不转人工，仅记录补全供前端展示。
            if field.startswith("llm_semantic.") or "ambiguity" in field.lower():
                a["auto_filled"] = True
                augmentations.append({
                    "field": field,
                    "source_req_id": best["id"],
                    "similarity": best["similarity"],
                    "value": value,
                    "_kb_answered_only": True,
                })
                continue

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
                # v2.8.53: LLM 裁决成功的冲突（llm_*）使用裁决值写入 SRR，不转人工
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

        # v2.8.4: PG 关系增强仅查询 KB（已归档）数据，未归档需求不参与。
        # 原因：PG 增强为 KB 自动补全提供上下文，KB 仅含 archived 需求；
        #       若产品尚无归档需求，以下查询应全部返回空，避免误导。
        _ARCHIVED = "archived"

        # 1. 查询当前需求的 PG 记录，获取 project_id 和 tags
        try:
            stmt = select(Requirement).where(Requirement.id == current_req_id)
            result = await session.execute(stmt)
            current_record = result.scalar_one_or_none()
        except Exception:
            current_record = None

        project_id = current_record.project_id if current_record else None

        # 2. 同项目下的其他需求（仅归档状态，显式 product_id 隔离）
        if project_id:
            try:
                stmt = select(Requirement.id, Requirement.title, Requirement.priority).where(
                    Requirement.project_id == project_id,
                    Requirement.product_id == product_id,
                    Requirement.id != current_req_id,
                    Requirement.status == _ARCHIVED,
                ).limit(10)
                rows = (await session.execute(stmt)).all()
                context["same_project_reqs"] = [
                    {"id": r[0], "title": r[1], "priority": r[2]} for r in rows
                ]
            except Exception:
                pass

        # 3. 同模块下的其他需求（仅归档状态，已按 product_id 过滤）
        if current_modules:
            try:
                stmt = select(Requirement.id, Requirement.title, Requirement.modules).where(
                    Requirement.id != current_req_id,
                    Requirement.product_id == product_id,
                    Requirement.status == _ARCHIVED,
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

        # 4. 共享标签（仅归档状态，已按 product_id 过滤）
        if current_record and current_record.tags:
            try:
                stmt = select(Requirement.id, Requirement.title, Requirement.tags).where(
                    Requirement.id != current_req_id,
                    Requirement.product_id == product_id,
                    Requirement.status == _ARCHIVED,
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
        # v2.8.4: 通过 Module 表按 product_id 过滤，确保产品隔离。
        #   原 bug：current_modules 可能是模块名称（非 ID），直接 .in_(source_module_id) 匹配失败
        #          或匹配到其他产品的同名模块。现先解析为当前产品的模块 ID 再查询。
        if current_modules and product_id:
            try:
                # 查询当前产品的所有模块，建立 id/name 双向映射
                mod_stmt = select(Module.id, Module.name).where(Module.product_id == product_id)
                mod_rows = (await session.execute(mod_stmt)).all()
                product_module_ids = {r[0] for r in mod_rows}
                name_to_id = {r[1]: r[0] for r in mod_rows}

                # 将 current_modules（可能是名称或 ID）解析为当前产品的模块 ID
                matched_module_ids: set[str] = set()
                for m in current_modules:
                    if m in product_module_ids:
                        matched_module_ids.add(m)  # 已经是 ID
                    if m in name_to_id:
                        matched_module_ids.add(name_to_id[m])  # 名称 → ID

                if matched_module_ids:
                    stmt = select(ModuleDependency).where(
                        ModuleDependency.source_module_id.in_(matched_module_ids)
                        | ModuleDependency.target_module_id.in_(matched_module_ids)
                    ).limit(20)
                    deps = (await session.execute(stmt)).scalars().all()
                    # 二次过滤：仅保留源和目标都在当前产品内的依赖（严格产品隔离）
                    context["module_deps"] = [
                        {
                            "source": d.source_module_id,
                            "target": d.target_module_id,
                            "relation_type": d.relation_type,
                            "strength": d.strength,
                        }
                        for d in deps
                        if d.source_module_id in product_module_ids
                        and d.target_module_id in product_module_ids
                    ]
            except Exception:
                pass

        # 6. 已有的追溯矩阵条目（检索历史用例与需求的关联）
        # v2.8.3: JOIN Requirement 按 product_id 过滤
        # v2.8.4: 增加 status='archived' 过滤，仅返回 KB 已归档需求的追溯记录；
        #          排除当前需求自身（未归档，不应出现在 KB 上下文中）
        try:
            stmt = (
                select(Traceability)
                .join(Requirement, Traceability.requirement_id == Requirement.id)
                .where(
                    Requirement.product_id == product_id,
                    Requirement.status == _ARCHIVED,
                    Requirement.id != current_req_id,
                )
                .limit(50)
            )
            traces = (await session.execute(stmt)).scalars().all()
            context["existing_traces"] = [
                {"test_case_id": t.test_case_id, "requirement_id": t.requirement_id,
                 "trace_type": t.trace_type, "confidence": t.confidence}
                for t in traces
            ]
        except Exception:
            pass

        # 7. 产品上下文（产品元信息，非 KB 数据，无需 status 过滤）
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
            completeness=completeness,
            ambiguities=ambiguities,
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

            # v2.8.62: 如果 LLM 检测到问题但未生成 questions，则基于 issues_detected 转换
            # 原因：部分 LLM 模型只返回 issues_detected 而不生成 questions，
            # 导致检测到的跨节矛盾、引用未定义等问题被完全丢弃。
            if not questions and result.get("issues_detected"):
                issues = result.get("issues_detected", [])
                for i, issue in enumerate(issues, 1):
                    severity = issue.get("severity", "important")
                    if severity == "optional":
                        continue
                    issue_type = issue.get("type", "gap")
                    description = issue.get("description", "检测到未明确的点，请补充说明")
                    field = issue.get("field", "")
                    if issue_type == "conflict":
                        input_type = "select"
                        options = ["采用方案A", "采用方案B", "需统一为其他表述"]
                    else:
                        input_type = "text"
                        options = []
                    questions.append({
                        "id": f"Q{i:03d}",
                        "field": field,
                        "priority": severity,
                        "category": issue_type,
                        "question": description,
                        "context": f"LLM 语义检测发现{issue_type}：{description}",
                        "default_answer": "",
                        "input_type": input_type,
                        "options": options,
                    })
                if questions:
                    logger.info(
                        "[ClarifyAgent] LLM 未生成 questions，从 issues_detected 转换 {} 个问题",
                        len(questions),
                    )

            # v2.8.66: LLM 问题源头做必要性过滤——猜想型 upper_bound_rule 不在下游产生
            # （与人工澄清阶段复用同一套函数，避免逻辑分叉）
            questions = self._filter_questions_by_necessity(questions)
            # v2.8.67: 过滤「过度宽泛的设计阶段细节」追问（实体字段定义/happy_path步骤细化/
            # 逐功能点性能穷举）--这些维度原始需求未要求时不应打扰用户
            questions = self._filter_over_broad_llm_questions(questions, srr)
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
    # v2.8.53: 拆分强/弱优化关键词，避免 UI 描述中的"调整/完善/增强"误判
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

    # ========================================================================
    # 新建功能 / PRD 专项检测（v2.8.64 新增）
    # ========================================================================
    # 对应「优化需求专项检测」_detect_optimization_impact 的反向专项：
    # 当需求为「新建功能 / PRD / 全新系统」（is_optimization=False）时，
    # 对 _raw_text 原文执行 5 项规则层前置检测，把 PRD 常见的结构性细节缺口
    # （上限无处理规则 / 降级无触发条件 / 风险能力不一致 / 立场不一致 / 模糊条件）
    # 先在规则层识别为 completeness["prd_missing"] 项，再注入 completeness，
    # 确保后续 _generate_questions 的 LLM 调用能看到这些缺口，不会只给 overall=1.0 满分。

    def _detect_prd_specific_gaps(self, srr: dict) -> tuple[list[dict], list[dict]]:
        """对新建功能/PRD 需求的原文执行 5 项专项检测（规则层）。

        Args:
            srr: SRR 字典（必须含 _raw_text 原文）

        Returns:
            (prd_missing, prd_ambiguities):
                prd_missing: list[dict] —— 语义遗漏类（gap），每项:
                    {"field": 字段路径, "description": 缺口描述, "category": "gap",
                     "evidence": 原文片段证据, "suggested_question": 建议问句,
                     "suggested_options": 建议候选选项}
                prd_ambiguities: list[dict] —— 歧义/矛盾类（ambiguity/conflict），
                    结构同上
        """
        raw_text = srr.get("_raw_text", "") or ""
        prd_missing: list[dict] = []
        prd_ambiguities: list[dict] = []
        if not raw_text:
            return prd_missing, prd_ambiguities

        import re

        # ------------------------------------------------------------------
        # a) 上限/数量约束的落地规则缺失（只说了保留N个，但没说达到上限后的行为）
        # ------------------------------------------------------------------
        # 匹配模式：「保留最近 N 版」「最多 N 条」「上限 N」「最多 N 个」「超时 N 分钟」等
        upper_bound_pattern = re.compile(
            r'(保留最近?|最多|上限|最多保留|不超过|最多不超过|最近)?\s*'
            r'(\d+)\s*(版|条|个|条记录|分钟|秒|小时|天|日|MB|GB|KB)'
            r'(?!.{0,40}(?:删除|清理|覆盖|淘汰|FIFO|LRU|阻止|提示|超出处理|自动去掉|自动移除|自动清理))',
            re.IGNORECASE,
        )
        for m in upper_bound_pattern.finditer(raw_text):
            # 找到匹配的上下文（前后 80 字符）
            start = max(0, m.start() - 40)
            end = min(len(raw_text), m.end() + 80)
            context_snippet = raw_text[start:end].replace('\n', ' ')
            bound_desc = m.group(0).strip()
            # 避免对同一个约束重复添加（同一句的多次匹配）
            dup_key = f"upperbound_{bound_desc}"
            if any(d.get("_dup") == dup_key for d in prd_missing):
                continue
            prd_missing.append({
                "_dup": dup_key,
                "field": f"prd.upper_bound_rule.{bound_desc}",
                "category": "gap",
                "description": f"声明了「{bound_desc}」的数量约束，但未定义达到/超过该上限后的处理行为（淘汰策略/阻止操作/用户通知/管理员可调）",
                "evidence": f"上下文：…{context_snippet}…",
                "suggested_question": f"文档声明「{bound_desc}」，但未说明达到该上限后系统的处理行为。请明确：达到上限后应如何处理？",
                "suggested_options": [
                    "自动删除最旧的条目后继续保存（FIFO策略）",
                    "阻止操作并提示用户清理后重试",
                    "允许用户选择要删除/覆盖的条目",
                    "覆盖最旧条目且不提示用户",
                ],
            })

        # ------------------------------------------------------------------
        # b) 降级/后备方案的触发与恢复细节缺失
        # ------------------------------------------------------------------
        degrade_patterns = [
            (r'(若|如果|当|无法|不能|不可|缺少|缺失|不支持).{0,20}(则|就|则改为|则使用|降级为|退回|备用|后备|默认|改为|回退到).{0,40}',
             '降级条件模式'),
            (r'(fallback|degrade|降级|后备|备用|备选).{0,60}', '降级关键词模式'),
        ]
        seen_degrade = set()
        for pat, _label in degrade_patterns:
            for m in re.finditer(pat, raw_text):
                start = max(0, m.start() - 60)
                end = min(len(raw_text), m.end() + 120)
                snippet = raw_text[start:end].replace('\n', ' ')
                # 检查是否提到了触发条件、通知、恢复三件事中的任何一件
                has_trigger = any(w in snippet for w in ['启动', '加载', '初始化', '首次', '运行时', '检测', '判断', '条件'])
                has_notify = any(w in snippet for w in ['提示', '通知', '告警', '展示', '用户', '管理员'])
                has_recover = any(w in snippet for w in ['恢复', '自动切回', '重试', '重新检测', '切回主', '恢复主'])
                missing_items = []
                if not has_trigger:
                    missing_items.append('触发条件判定标准（何时触发）')
                if not has_notify:
                    missing_items.append('用户/管理员通知方式')
                if not has_recover:
                    missing_items.append('条件恢复后的切回机制')
                if missing_items:
                    dup_key = f"degrade_{snippet[:60]}"
                    if dup_key in seen_degrade:
                        continue
                    seen_degrade.add(dup_key)
                    prd_ambiguities.append({
                        "_dup": dup_key,
                        "field": f"prd.degrade_rule.{len(prd_ambiguities)+1}",
                        "category": "ambiguity",
                        "description": f"描述了降级/后备方案，但缺少以下细节：{', '.join(missing_items)}",
                        "evidence": f"上下文：…{snippet[:200]}…",
                        "suggested_question": f"文档中提到了降级/后备方案，但缺少{', '.join(missing_items)}。请补充：该降级方案的触发时机、通知方式、恢复条件分别是什么？",
                    })

        # ------------------------------------------------------------------
        # d) 功能范围声明 vs 风险缓解建议的立场不一致
        # ------------------------------------------------------------------
        # 先找「范围外/不包含/不做/不含/Out of Scope/非本次范围」声明
        scope_out_sections: list[tuple[int, str]] = []
        for m in re.finditer(r'(?:功能范围|Scope|范围)[^：:]*[：:][^\n]*?(?:不包含|不含|不做|范围外|非本次|Out of Scope|不支持)[^\n]*', raw_text, re.IGNORECASE):
            scope_out_sections.append((m.start(), m.group(0).replace('\n', ' ')))
        # 找「建议提供/建议实现/建议最小/建议XX入口/最小方案/应急处理/运营处置/手动XX」类风险缓解
        risk_suggestions: list[tuple[int, str]] = []
        for m in re.finditer(
            r'(?:建议|最小化方案|最小方案|应急措施|风险缓解|处置入口|运营处理|后备方案|补救措施)[^。；\n]{10,120}',
            raw_text,
        ):
            risk_suggestions.append((m.start(), m.group(0).replace('\n', ' ')))

        # 语义关键词交叉比对：范围外声明的关键词是否在建议中作为建议内容出现
        scope_keywords_list = ['管理后台', '内容审核', '内容安全', '后台', '管理员', '运营', '审核', '人工处理', '举报处理']
        for _s_pos, scope_text in scope_out_sections:
            # 提取该范围外声明包含哪些关键词
            out_kws = [kw for kw in scope_keywords_list if kw in scope_text]
            if not out_kws:
                continue
            # 看风险建议中是否出现同一关键词
            for _r_pos, risk_text in risk_suggestions:
                overlap = [kw for kw in out_kws if kw in risk_text]
                if not overlap:
                    continue
                # 立场不一致
                dup_key = f"stance_{'|'.join(overlap)}"
                if any(d.get("_dup") == dup_key for d in prd_ambiguities):
                    continue
                prd_ambiguities.append({
                    "_dup": dup_key,
                    "field": f"prd.stance_conflict.{overlap[0]}",
                    "category": "conflict",
                    "description": f"功能范围声明「不包含/不做{overlap[0]}」与风险缓解「建议提供{overlap[0]}最小方案/处置入口」存在立场不一致，导致实现预期不确定",
                    "evidence": f"范围声明：「{scope_text[:120]}」 vs 风险建议：「{risk_text[:120]}」",
                    "suggested_question": f"文档功能范围声明「不包含/不做{overlap[0]}」，但风险缓解又建议「提供{overlap[0]}最小方案/处置入口」，二者立场不一致。请明确：最终应如何处理？",
                    "suggested_options": [
                        "按范围声明不实现该能力，风险另寻外部方案处理",
                        "按风险建议补充最小化方案作为可选项",
                        "该最小方案独立为后续版本，v1.0 暂不交付",
                    ],
                })

        # ------------------------------------------------------------------
        # c) 风险评估能力 vs 权限模型一致性（简化版关键词比对）
        # ------------------------------------------------------------------
        # 找「运营/管理员/审核员/处理者/处置」等需要人工操作能力的风险描述
        human_cap_keywords = ['运营', '管理员', '审核员', '人工处理', '处置', '处理入口', '手动回滚', '手动恢复', '手动删除']
        has_human_cap_risk = False
        risk_human_snippet = ""
        for kw in human_cap_keywords:
            for m in re.finditer(kw, raw_text):
                start = max(0, m.start() - 40)
                end = min(len(raw_text), m.end() + 80)
                risk_human_snippet = raw_text[start:end].replace('\n', ' ')
                # 判断是否在风险/应急/缓解上下文中
                if any(w in risk_human_snippet.lower() for w in ['风险', '缓解', '应急', '处置', '举报', '违规', '回滚', '恢复', '补救']):
                    has_human_cap_risk = True
                    break
            if has_human_cap_risk:
                break
        if has_human_cap_risk:
            # 检查是否定义了对应的角色（含运营/管理员/审核/超级管理员/后台）
            role_definitions = []
            for rm in re.finditer(r'(角色权限|权限模型|用户角色|角色体系)[^：:]*[：:][^\n]{0,200}', raw_text):
                role_definitions.append(rm.group(0).replace('\n', ' '))
            for rm in re.finditer(r'角色[：:]\s*\{[^}]{0,500}\}', raw_text):
                role_definitions.append(rm.group(0).replace('\n', ' '))
            roles_text = ' '.join(role_definitions)
            # 检查：是否明确不含管理员/后台，或者是否没有任何运营/管理员角色设计
            no_admin_scope = any(w in scope_text for _, scope_text in scope_out_sections
                                 for w in ['管理后台', '管理员', '后台', '内容审核'])
            no_admin_in_roles = not roles_text or not any(
                rkw in roles_text for rkw in ['运营', '管理员', '超级管理员', '审核员', '后台']
            )
            if no_admin_scope or no_admin_in_roles:
                dup_key = "prd_cap_risk_gap"
                if not any(d.get("_dup") == dup_key for d in prd_missing):
                    prd_missing.append({
                        "_dup": dup_key,
                        "field": "prd.role_capability_gap",
                        "category": "gap",
                        "description": "风险缓解要求有运营/管理员的处置能力，但权限模型和功能范围中没有定义对应角色或操作入口",
                        "evidence": f"风险处置上下文：…{risk_human_snippet[:200]}… | 权限/范围：{(roles_text[:150] if roles_text else '未定义角色权限')} | {(scope_out_sections[0][1][:120] if scope_out_sections and no_admin_scope else '')}",
                        "suggested_question": "风险评估中要求有运营/管理员处置能力（如手动处理违规、手动回滚版本等），但权限模型和功能范围中未定义对应角色或操作入口。请明确：这些人工操作的执行主体和入口是什么？",
                        "suggested_options": [
                            "新增独立运营/审核员角色及后台入口",
                            "由超级管理员账号统一处理（最小方案）",
                            "通过数据库脚本/后台运维工具处理（仅内部）",
                            "调整为后续版本迭代，v1.0 暂不支持",
                        ],
                    })

        # ------------------------------------------------------------------
        # e) 模糊条件词缺少判定主体和标准（简化版）
        # ------------------------------------------------------------------
        vague_cond_words = ['必要时', '适当', '视情况而定', '按需', '有需要时', '如需要', '合理范围内']
        for vw in vague_cond_words:
            for m in re.finditer(re.escape(vw), raw_text):
                start = max(0, m.start() - 50)
                end = min(len(raw_text), m.end() + 100)
                snippet = raw_text[start:end].replace('\n', ' ')
                # 检查是否有判定主体（系统/用户/管理员/运营/算法）和判定标准（阈值/枚举场景）
                has_subject = any(w in snippet for w in ['系统', '用户', '管理员', '运营', '算法', '自动'])
                has_standard = any(w in snippet for w in ['当', '若', '如果', '阈值', '条件', '场景包括', '例如'])
                if not has_subject or not has_standard:
                    dup_key = f"vague_{vw}_{snippet[:40]}"
                    if any(d.get("_dup") == dup_key for d in prd_ambiguities):
                        continue
                    prd_ambiguities.append({
                        "_dup": dup_key,
                        "field": f"prd.vague_condition.{len(prd_ambiguities)+1}",
                        "category": "ambiguity",
                        "description": f"使用了模糊条件词「{vw}」但未明确{('判定主体' if not has_subject else '')}{('和' if (not has_subject and not has_standard) else '')}{('判定标准' if not has_standard else '')}",
                        "evidence": f"上下文：…{snippet[:160]}…",
                        "suggested_question": f"文档中使用了「{vw}」这类模糊表述，但未明确判定主体和判定标准。请补充：「{vw}」的具体判定条件是什么？由谁判断？",
                        "suggested_options": [
                            "由系统按预设规则自动判定（需补充具体规则）",
                            "仅由人工运营/管理员判断",
                            "系统预设规则 + 管理员最终复核",
                        ],
                    })

        # 清理临时 _dup 键
        for lst in (prd_missing, prd_ambiguities):
            for item in lst:
                item.pop("_dup", None)

        if prd_missing or prd_ambiguities:
            logger.info(
                "[ClarifyAgent] PRD 专项检测（v2.8.64）: gaps={}  ambiguities={}",
                len(prd_missing), len(prd_ambiguities),
            )
        return prd_missing, prd_ambiguities

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
        # v3.0.1 跨条目矛盾按主题合并：同一约束主题（th）下的多处冲突收集后
        # 合并为一条问题，避免 O(n²) 对同一主题的多对条目生成多条重复问题。
        theme_conflicts: dict[str, list[dict]] = {}
        req = srr.get("requirement", {}) or {}
        req_title = req.get("title", "当前需求") or "当前需求"

        # v2.8.70 收敛修复：跳过已在澄清历史中回答过的「引用未定义」主题
        # 根因：ref_undef 检测基于原始文档原文（_raw_text）里的「沿用现有XX」表述，
        # 用户回答后原文不变，导致「图片上传/权限控制」等主题每轮重复提问，直至 3 轮强制放行。
        # 修复：读取 _clarify_history 中已展示问题的 _ref_theme_label，收集「已答主题」，
        # 本轮生成 ref_undef 时若主题与已答主题重叠则跳过，实现收敛。
        resolved_themes: set[str] = set()
        try:
            _hist = srr.get("_clarify_history") if isinstance(srr, dict) else None
            if isinstance(_hist, list):
                for _entry in _hist:
                    if not isinstance(_entry, dict):
                        continue
                    for _q in (_entry.get("questions") or []):
                        if not isinstance(_q, dict):
                            continue
                        _tl = _q.get("_ref_theme_label") or ""
                        if isinstance(_tl, str) and _tl:
                            resolved_themes.add(_tl)
                        else:
                            # v3.0.1: 兜底——历史问题未带 _ref_theme_label 时，
                            # 若为引用未定义类问题，从问题「」中抽取主题纳入已答主题集合，
                            # 避免同一引用未定义主题第二轮仍重复提问。
                            _qt = str(_q.get("question", "") or "")
                            _ctx = str(_q.get("context", "") or "")
                            _hay = f"{_qt} {_ctx}"
                            _is_ref = ("引用未定义" in _ctx
                                       or "沿用" in _hay
                                       or ("现有" in _hay and ("规则" in _hay or "一致" in _hay)))
                            if _is_ref:
                                import re as _re_th
                                _m = _re_th.search(r"「([^」]{1,20})」", _qt or _ctx)
                                if _m:
                                    resolved_themes.add(_m.group(1))
        except Exception:
            resolved_themes = set()

        # 主题重叠判断：allow 已知主题关键词重叠（如「图片上传」vs「图片上传规则」）
        _REF_KNOWN_THEMES = (
            "图片上传", "上传规则", "素材上传", "文件上传", "附件上传",
            "敏感词校验", "敏感词", "权限控制", "访问控制", "角色权限",
            "版本保留", "版本管理", "改动比例", "小修订", "审批流程",
            "淘汰规则", "限流规则", "自动保存", "归档规则",
        )

        def _theme_overlaps(a: str, b: str) -> bool:
            if not a or not b:
                return False
            if a == b:
                return True
            _ak = {k for k in _REF_KNOWN_THEMES if k in a}
            _bk = {k for k in _REF_KNOWN_THEMES if k in b}
            return bool(_ak & _bk)

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

        # ====================================================================
        # v2.8.65 新增能力 A：从 _raw_text（Markdown）解析所有结构化/半结构化列表条目
        # （- [ ] / - / 1. 等），作为"验收标准候选条目"参与后续检测
        # 原因：很多需求仅在 Markdown 文本里用列表写验收标准，并未结构化到
        #       acceptance_criteria 字段，之前的结构化扫描会完全漏掉
        # ====================================================================
        candidate_items: list[tuple[str, str]] = []  # (条目正文, 来源标签)

        def _add_item(txt: str, src: str):
            if not txt or len(txt) < 4:
                return
            # 去掉 Markdown 复选框前缀：- [x] / - [ ] / - / 1. 2. / *
            cleaned = re.sub(r'^\s*(?:[-*]\s*(?:\[[ x]\]\s*)?|\d+\.\s*)', '', txt.strip())
            cleaned = cleaned.strip()
            if len(cleaned) >= 4:
                candidate_items.append((cleaned, src))

        # 来源1：fp_summary 数组（结构化SRR的功能点描述，含"最多50个"等）
        for fp in (srr.get("fp_summary") or [])[:50]:
            if isinstance(fp, dict):
                for _f in ["name", "description", "acceptance"]:
                    t = fp.get(_f, "") or ""
                    if t:
                        _add_item(str(t), f"功能点{fp.get('name','')[:12]}(fp_summary)")
                # flows 内嵌 action 扫描
                if isinstance(fp.get("prd_paragraph"), str):
                    for line in str(fp["prd_paragraph"]).splitlines():
                        _add_item(line, f"fp.{fp.get('name','')[:10]}.prd")
        # 来源2：_raw_text 的所有 Markdown 行（- xxx / 1. xxx / - [ ] xxx）
        if raw_text:
            in_section = None
            for lineno, line in enumerate(raw_text.splitlines(), 1):
                s = line.rstrip()
                # 记录所在 Markdown 二级节，便于来源精准定位
                hdr = re.match(r'^(#{1,6})\s*(.+?)\s*#*\s*$', s)
                if hdr:
                    in_section = hdr.group(2).strip()[:24]
                    continue
                src = f"raw_text#{lineno}" + (f"[{in_section}]" if in_section else "")
                if re.match(r'^\s*(?:[-*]\s*(?:\[[ x]\]\s*)?|\d+\.\s*)\S', s):
                    _add_item(s, src)
                # 非列表但含"验收标准/约束/规则"字样的长行也收录
                elif len(s) > 15 and ("验收" in s or "规则" in s or "约束" in s or "上限" in s or "版本" in s):
                    _add_item(s, src)
        # 来源3：cross_fp 下 shared_business_rules / global_constraints / flows
        for r in br if isinstance(br, list) else []:
            t = r.get("rule","") or r.get("description","") if isinstance(r, dict) else str(r)
            _add_item(t, "shared_business_rules")
        for g in gc if isinstance(gc, list) else []:
            _add_item(str(g), "global_constraints")

        # ====================================================================
        # v2.8.65 新增能力 B：候选条目做 O(n²) 两两语义冲突检测
        # 重点：数量/上限无限制、开关、权限、时间有效期 等对反
        #   ★ 误报防护：开关/权限类必须两个条目说的是「同一个功能能力」才判定
        #     判定方式：共同能力锚点词重叠（同一功能名/模式名/面板名等）
        # ====================================================================
        # 同一"约束主题"的关键词聚合（用于判断两条在说同一个事）
        THEME_BUCKETS: list[tuple[str, set[str]]] = [
            ("版本保留数量/上限", {
                "版本保留","保留版本","版本数量","版本上限","保留N个","保留数量","版本 个",
                "手动标记版本","清理最旧","超出提示","最多保留","保留","数量上限","不设上限","无限制","不限数量",
            }),
            ("图片上传/素材库", {
                "素材库","上传规则","图片格式","图片大小","单文件大小","文件大小","支持的图片格式",
                "沿用现有上传","与现有图片上传","格式","大小限制",
            }),
            ("开关类", {"开启","关闭","启用","禁用","默认开启","默认关闭","必须","不提供"}),
            ("权限类", {"管理员","作者本人","仅作者","所有用户","仅管理员","普通用户","运营"}),
            ("时间/有效期", {"永久有效","自动过期","30天过期","过期","有效期"}),
        ]

        # ==================================================================
        # O(n²) 保护：候选条目数过大时只保留「携带主题敏感关键词」的条目
        # (1) 先挑出带 THEME_BUCKETS 任何主题关键词 + 引用未定义关键词的条目（最多 MAX_THEMED）
        # (2) 不够 MAX_THEMED 再补其他条目到 MAX_ITEMS，避免过大文档 O(n²)=40000+
        # ==================================================================
        MAX_ITEMS = 200
        MAX_THEMED = 160
        _theme_kws_all: set[str] = set()
        for _nm, _kws in THEME_BUCKETS:
            _theme_kws_all.update(_kws)
        _theme_kws_all.update({"沿用","现有","一致","参照现有","遵循现有","与现有"})  # 引用未定义强关键词（避免"遵循原则"这类合规术语误收录）
        prio_list: list[tuple[str, str]] = []
        rest_list: list[tuple[str, str]] = []
        for it in candidate_items:
            if any(k in it[0] for k in _theme_kws_all):
                prio_list.append(it)
            else:
                rest_list.append(it)
        prio_list = prio_list[:MAX_THEMED]
        remain_slot = MAX_ITEMS - len(prio_list)
        if remain_slot > 0:
            prio_list.extend(rest_list[:remain_slot])
        candidate_items = prio_list
        if len(candidate_items) >= MAX_ITEMS:
            logger.info(
                "[ClarifyAgent] 候选条目较多，已截断到 {} 条（优先保留主题敏感条目）以控制O(n²)开销",
                MAX_ITEMS,
            )

        def _which_theme(txt: str) -> list[str]:
            return [name for name, kws in THEME_BUCKETS if any(kw in txt for kw in kws)]

        # 能力锚点词：用于判断两个条目说的是不是同一个功能（防误报）
        # 提取规则：从"xxx模式""xxx功能""xxx菜单""xxx面板""xxx版本""xxx权限""xxx按钮"等结构中的中心词
        CAP_ANCHOR_RE = re.compile(r"([\u4e00-\u9fa5A-Za-z0-9_]{2,20})(?:模式|功能|菜单|面板|抽屉|版本|按钮|弹窗|开关|页|模块|能力|组件|视图)")
        CAP_HINT_WORDS_RE = re.compile(r"(专注模式|打字机模式|命令菜单|素材库|历史版本|手动标记|版本标记|自动保存版本|预览模式|小修订|通知|冲突处理|大纲导航|拦截替换|举报处置|敏感词)")

        def _cap_anchors(txt: str) -> set[str]:
            anchors: set[str] = set()
            for m in CAP_ANCHOR_RE.finditer(txt):
                w = m.group(1) + (m.group(0)[len(m.group(1)):])  # 把后缀加上
                if len(w) >= 3:
                    anchors.add(w)
            for m in CAP_HINT_WORDS_RE.finditer(txt):
                anchors.add(m.group(1))
            # 如果有in_section节名，也作为锚点
            return anchors

        # 语义相反表达词典（theme ->  [(pattern_A, pattern_B), ...]）
        OPPOSITE_PAIRS: dict[str, list[tuple[re.Pattern, re.Pattern]]] = {
            "版本保留数量/上限": [
                # 有上限 vs 不设上限
                (re.compile(r"最\s*多\s*保\s*留\s*\d+\s*个|保\s*留\s*\d+\s*个|上\s*限\s*\d+|\d+\s*个\s*(为止|以内|上限)"),
                 re.compile(r"不\s*设\s*(数\s*量\s*)?上\s*限|无\s*(数\s*量\s*)?限\s*制|不\s*限\s*数\s*量|长\s*期\s*保\s*留.*不\s*设|无\s*上\s*限")),
            ],
            "开关类": [
                # 严格模式：仅当两条都在声明"默认状态/必须开启/必须关闭/不提供功能"才算冲突
                # 注意：不要把"开启后xxx如何"的条件词匹配成"默认开启"
                (re.compile(r"必\s*须\s*开\s*启|默\s*认\s*开\s*启|应\s*开\s*启|要\s*开\s*启|默\s*认\s*为\s*开|常\s*开|默\s*认\s*启用"),
                 re.compile(r"默\s*认\s*关\s*闭|不\s*提\s*供\s*\S{0,10}功\s*能|禁\s*用|不\s*能\s*开\s*启|默\s*认\s*关|关\s*闭\s*为\s*默\s*认|常\s*闭")),
            ],
            "权限类": [
                # 权限类也做严格限定：声明角色范围A vs 声明角色范围B 才算冲突
                (re.compile(r"仅\s*管\s*理\s*员|管\s*理\s*员\s*才\s*能|只\s*有\s*管\s*理\s*员"),
                 re.compile(r"所\s*有\s*用\s*户|普\s*通\s*用\s*户|每\s*个\s*用\s*户|用\s*户\s*均\s*可|任\s*何\s*人")),
                (re.compile(r"仅\s*作\s*者|仅\s*本\s*人|作\s*者\s*本\s*人|仅\s*自\s*己"),
                 re.compile(r"公\s*开|所\s*有\s*人|全\s*站\s*可\s*见|大\s*家\s*都\s*能\s*看")),
            ],
            "时间/有效期": [
                (re.compile(r"永\s*久\s*有\s*效|永\s*不\s*过\s*期|长\s*期\s*有\s*效"),
                 re.compile(r"\d+\s*天.*(过\s*期|失\s*效|自\s*动\s*清)|过\s*期.*\d+\s*天|有\s*效\s*期\s*\d+")),
            ],
        }

        # 预计算每个条目的能力锚点（避免重复）
        items = candidate_items
        item_anchors = [_cap_anchors(t) for t, _ in items]
        n = len(items)
        for i in range(n):
            ti, si = items[i]
            themes_i = _which_theme(ti)
            if not themes_i:
                continue
            anc_i = item_anchors[i]
            for j in range(i+1, n):
                tj, sj = items[j]
                common_themes = [th for th in themes_i if th in _which_theme(tj)]
                if not common_themes:
                    continue
                anc_j = item_anchors[j]
                # ============== 误报防护：开关/权限类必须有共同能力锚点 ==============
                strict_themes = {"开关类", "权限类"}
                # 如果任一主题是严格主题，要求：
                #   1) 两条目共同锚点 ≥ 1（同一功能名），OR
                #   2) 同一 in_section（Markdown节名一致）且至少有一个其他共同非主题词
                if any(th in strict_themes for th in common_themes):
                    shared_anchors = anc_i & anc_j
                    # 提取共同的非主题词（长度≥2）：作为额外的弱信号
                    shared_tokens = set(re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,}", ti)) & set(re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]{2,}", tj))
                    # 去掉只属于theme本身的词
                    for _th in common_themes:
                        for _kw in next((kws for nm, kws in THEME_BUCKETS if nm == _th), set()):
                            if _kw in shared_tokens:
                                shared_tokens.discard(_kw)
                    same_section = False
                    if "[" in si and "[" in sj:
                        try:
                            sec_i = si.split("[",1)[1].rstrip("]")
                            sec_j = sj.split("[",1)[1].rstrip("]")
                            same_section = (sec_i == sec_j and len(sec_i) > 2)
                        except Exception:
                            pass
                    if not (shared_anchors or (same_section and len(shared_tokens) >= 1)):
                        continue  # 不同功能，跳过（防误报）
                for th in common_themes:
                    for patA, patB in OPPOSITE_PAIRS.get(th, []):
                        mA = patA.search(ti)
                        mB = patB.search(tj)
                        # 正反方向都要试：A在ti/B在tj，或A在tj/B在ti
                        mA2 = patA.search(tj)
                        mB2 = patB.search(ti)
                        hit = (mA and mB) or (mA2 and mB2)
                        if not hit:
                            continue
                        side_A = (mA.group(0) if mA else (mA2.group(0) if mA2 else "")).strip()
                        side_B = (mB.group(0) if mB else (mB2.group(0) if mB2 else "")).strip()
                        dedup_key = f"cross_item::{th}::{side_A}::{side_B}"
                        if dedup_key in seen_contradictions:
                            continue
                        seen_contradictions.add(dedup_key)
                        logger.info(
                            "[ClarifyAgent] 条目级冲突命中 theme={}: 「{}...」vs「{}...」pair=({} vs {})",
                            th, ti[:40], tj[:40], side_A, side_B,
                        )
                        # v3.0.1 按主题收集，末尾统一合并为一条问题
                        theme_conflicts.setdefault(th, []).append({
                            "si": si, "sj": sj, "ti": ti, "tj": tj,
                            "side_A": side_A, "side_B": side_B,
                        })
                        break  # 一对条目面对同一 theme 只记录一对相反模式即可

        # ====================================================================
        # v2.8.65 新增能力 C：引用未定义的规则级兜底检测（语义而非纯关键词）
        #   核心改进：不能把条目的任何数字都当作"已定义具体内容"，要按主题-信号
        #   一一对应来判断。例如"2GB 总量配额"不能被认定为"单文件图片大小"已定义。
        # ====================================================================
        REF_UNDEF_VERBS = (
            # Level A (强引用信号，必触发)：明确说"沿用现有X / 与现有X一致 / 按现有X执行"
            r"沿\s*用\s*现\s*有|"
            r"与\s*现\s*有.*(一\s*致|完\s*全\s*一\s*致|保\s*持\s*一\s*致|相\s*同|无\s*差\s*异)|"
            r"按\s*现\s*有.*(执\s*行|处\s*理|规\s*则|流\s*程)|"
            r"使\s*用.*(?:现\s*有|既\s*有|原\s*系\s*统).*(?:规\s*则|流\s*程|约\s*束|限\s*制)|"
            r"采\s*用.*(?:现\s*有|既\s*有|原\s*系\s*统).*(?:规\s*则|流\s*程|约\s*束|限\s*制)|"
            r"与\s*(?:上\s*述|既\s*有|原\s*系\s*统|前\s*文).*(一\s*致|相\s*同|规\s*则|处\s*理)|"
            # Level B (弱引用信号)：只有带"现有/既有/原系统/XX一致"才触发，避免"遵循最小收集原则"类合规术语误报
            r"参\s*照\s*(?:现\s*有|既\s*有|原\s*系\s*统).*(?:规\s*则|流\s*程|限\s*制)|"
            r"遵\s*循\s*(?:现\s*有|既\s*有|原\s*系\s*统).*(?:规\s*则|流\s*程|限\s*制|约\s*束)|"
            r"基\s*于\s*(?:现\s*有|既\s*有|原\s*系\s*统).*(?:规\s*则|流\s*程)"
        )
        REF_UNDEF_RE = re.compile(REF_UNDEF_VERBS)

        # 主题 -> 对应"已定义具体内容"的信号正则（只有该主题下的信号才算）
        THEME_DEFINITION_SIGNALS: list[tuple[set[str], re.Pattern]] = [
            # 主题：素材库上传、图片格式、单文件大小 → 具体定义必须是"X MB/KB + 文件格式列表"
            (
                {"素材库","上传规则","图片格式","图片大小","单文件大小","文件大小","现有图片上传","图片上传"},
                re.compile(
                    r"((?:单\s*文\s*件|单\s*张|单\s*个|每\s*个\s*文\s*件|每\s*张|文\s*件\s*本\s*身)\s*(?:上\s*传|大\s*小|限\s*制)?\s*(?:最\s*大|不\s*超\s*过|≤|<|不\s*能\s*超\s*过)?\s*\d+\s*(?:MB|KB|M|K))"
                    r"|((?:支\s*持|允\s*许|格\s*式|包\s*括)\s*[:：]?\s*(?:jpg|jpeg|png|gif|webp|svg|pdf|bmp)(?:[\s,，/、]|$))",
                    re.IGNORECASE,
                ),
            ),
            # 主题：小修订、5%、改动比例、正文改动 → 必须有"按字符数/按段落数/按编辑距离/按单词数"等计算方式
            (
                {"小修订","正文改动","< 5%","<5%","改动比例","5%","通知触发"},
                re.compile(r"按\s*(?:字\s*符\s*数|段\s*落\s*数|编\s*辑\s*距\s*离|单\s*词\s*数|字\s*数|行\s*数|字\s*节|"
                           r"行\s*数|字|字\s*节\s*数)|"
                           r"(?:字\s*符\s*数|段\s*落\s*数|编\s*辑\s*距\s*离|单\s*词\s*数|字\s*数|行\s*数)\s*(?:"
                           r"计\s*算|统\s*计|核\s*算|判\s*断|作\s*为\s*标\s*准|为\s*判\s*定|来\s*度\s*量|衡\s*量|度\s*量)"),
            ),
            # 主题：版本标记、保留数量、版本上限 → 必须有"N个""最多X""清理策略"
            (
                {"手动标记版本","保留数量","版本上限","自动保存版本"},
                re.compile(r"\d+\s*个|最\s*多\s*\d+|最\s*多\s*保\s*留|FIFO|LRU|按\s*(?:时\s*间|版\s*本|创\s*建)\s*顺\s*序|清\s*理\s*最\s*旧|自\s*动\s*删\s*除|保\s*留\s*策\s*略"),
            ),
            # 主题：一般校验规则（默认兜底）——具体数值、枚举格式、阈值带单位
            # 注意：GB 级通常是总量配额（如 2GB/5GB），不算"单文件/单条目"的具体定义信号，故剔除
            (
                {"格式","大小","限制","校验","规则","标准","规范"},
                re.compile(
                    r"(\d+\s*(?:MB|KB|M|K|字\s*节|字|条|分\s*钟|小\s*时|天))"
                    r"|(?:支\s*持|包\s*括|列\s*举)\s*[:：]?\s*(?:jpg|jpeg|png|gif|webp|svg|pdf|doc|docx|xlsx|xls|csv|mp4|mp3|zip|rar)",
                    re.IGNORECASE,
                ),
            ),
        ]

        def _concrete_defined_match(txt: str, extra_ctx: str = "") -> bool:
            """主题敏感地判断是否存在该主题对应的具体定义内容信号。"""
            for theme_kws, signal_re in THEME_DEFINITION_SIGNALS:
                txt_has_theme = any(kw in txt for kw in theme_kws)
                ctx_has_theme = any(kw in (extra_ctx or "") for kw in theme_kws)
                self_match = signal_re.search(txt) if txt_has_theme else None
                # 上下文信号匹配必须 txt + extra_ctx 都命中了同一个 theme 关键词，防止跨主题污染
                # 反例：图片上传条目上下文因 idx 近而包含版本条目→错误命中版本主题信号
                ctx_match = signal_re.search(extra_ctx) if (extra_ctx and txt_has_theme and ctx_has_theme) else None
                if self_match or ctx_match:
                    return True
            return False

        # C. 引用未定义：沿用语义 + 主题匹配
        # =============================================================
        # 收集到的候选（先做同主题合并，避免同一图片上传主题分别在#51和#59各出一条重复问题）
        ref_undef_candidates: list[dict] = []

        for t, s in candidate_items:
            if not REF_UNDEF_RE.search(t):
                continue
            # 提取紧邻上下文用于主题-信号判定
            extra_ctx = ""
            if raw_text:
                idx = raw_text.find(t[:20])
                if idx >= 0:
                    extra_ctx = raw_text[max(0, idx-300): idx + len(t) + 600]
            # 如果条目中存在对应主题的具体定义信号 → 已定义
            self_defined = _concrete_defined_match(t)
            context_defined = _concrete_defined_match("", extra_ctx) if extra_ctx else False
            if self_defined or context_defined:
                continue
            # 计算这条候选命中了 THEME_DEFINITION_SIGNALS 里的哪几组（按组索引，粗粒度合并）
            # 例："图片上传"命中组0→group0；多条命中组0就合并
            theme_groups: set[int] = set()
            for i, (theme_kws, _) in enumerate(THEME_DEFINITION_SIGNALS):
                if any(kw in t for kw in theme_kws):
                    theme_groups.add(i)
            if not theme_groups:
                theme_groups = {-1}  # -1 = 未命中任何已知主题组（兜底桶）
            # key：最小的命中主题组号（作为"主主题"代表），避免同一主题内关键词不同导致分散
            key_sig = f"theme_grp_{min(theme_groups)}"
            ref_undef_candidates.append({
                "t": t, "s": s, "key_sig": key_sig,
            })

        # 同主题合并：key_sig相同的合并为一条，否则独立
        def _merge_group(group: list[dict]) -> tuple[list[str], list[str], str, str]:
            """返回 (texts, sources, question_suffix, ctx_suffix)，最后2个是合并后提示补充"""
            texts = [g["t"] for g in group]
            srcs = [g["s"] for g in group]
            # 合并2条及以上的话提到多来源
            q_suffix = ""
            c_suffix = ""
            if len(group) >= 2:
                q_suffix = f"（共 {len(group)} 处表述，内容语义相近，以下合并澄清）"
                c_suffix = f"（多来源汇总）：{'；'.join(srcs[:3])}" + ("；..." if len(srcs) > 3 else "")
            return texts, srcs, q_suffix, c_suffix

        # groupby: 按 key_sig 分桶（保持首次出现顺序）
        buckets: dict[str, list[dict]] = {}
        order: list[str] = []
        for cand in ref_undef_candidates:
            if cand["key_sig"] not in buckets:
                buckets[cand["key_sig"]] = []
                order.append(cand["key_sig"])
            buckets[cand["key_sig"]].append(cand)

        for k in order:
            group = buckets[k]
            texts, srcs, q_suffix, c_suffix = _merge_group(group)
            rep_t = texts[0]
            rep_s = srcs[0]
            # questions 里相同语义的引用未定义去重（包括来自 LLM 层的）
            # 判断重叠：任意一条文本前40字或命中主题与已有questions重叠即可
            # v2.8.68 修复：LLM语义层生成的引用未定义问题 category=completeness/gap，
            # 规则层兜底用 category=ambiguity，原判断只命中 category=="ambiguity" 导致
            # 两者重复出现（如"沿用现有图片上传规则"出现两条）。现放宽 category 限制，
            # 并增加"现有.*规则"关键词匹配，避免 LLM 和规则层分别提问。
            def _signals_undef(qtext: str, qctx: str) -> bool:
                hay = f"{qtext} {qctx}"
                return ("引用未定义" in qctx
                        or "沿用" in hay
                        or "现有" in hay and ("规则" in hay or "一致" in hay))

            any_q_text_in_old = any(
                _signals_undef(str(q.get("question", "")), str(q.get("context", "")))
                and any(
                    tt[:40] in str(q.get("question", ""))
                    or tt[:40] in str(q.get("context", ""))
                    for tt in texts
                )
                for q in questions
            )
            if any_q_text_in_old:
                continue
            seen_key = f"ref_undef::{k}"
            if seen_key in seen_contradictions:
                continue
            seen_contradictions.add(seen_key)
            logger.info(
                "[ClarifyAgent] 引用未定义规则级兜底命中: source={} key={} text={} (n={})",
                rep_s, k, rep_t[:90], len(group),
            )
            # 构建合并版 question 和 context
            # v2.8.67 精简：question 只保留主题 + 核心追问（用户一眼能懂），
            # 多来源/原文引用/详细解释全部放入 context，避免问太长太乱看不清。
            # 原问题过长根因：src_refs(来源raw_text#N) + short_texts_str(长原文引用)
            #   + multi_src_note(多处来源汇总) + 通用提示 四者全部塞进了 question，
            #   导致用户在人工澄清列表里要翻半天才能看到核心问啥。
            # v2.8.68 修复：当 k 是内部分组标识（theme_grp_N）时，不把它泄漏到
            # 用户可见的 question 中，而是从代表性原文或主题关键词中抽取 theme_label。
            raw_k_str = str(k).replace("ref_theme::", "")
            if raw_k_str.startswith("theme_grp_") or raw_k_str.startswith("ref_theme::theme_grp_"):
                # 从代表原文抽取前32字并去冗余作为兜底主题名
                sample = (rep_t or "").strip()
                sample_clean = (sample[:40] if len(sample) > 40 else sample)
                # 去掉"沿用""保持一致"等声明词，保留真正的被引用主题名词
                strip_words = ["沿用", "保持", "现有", "与", "一致", "规则", "完全", "本次", "不改动", "（", "）"]
                fallback = sample_clean
                for w in strip_words:
                    fallback = fallback.replace(w, "")
                fallback = fallback.strip("：:,，。.;；、 \t\r\n")
                if not fallback:
                    fallback = "引用的规则"
                # 从分组文本中再抽取明显主题关键词（优先级更高）
                keywords_ordered = []
                for kw in ["图片上传", "上传规则", "素材上传", "敏感词校验", "敏感词",
                           "权限控制", "访问控制", "版本保留", "改动比例", "小修订",
                           "审批流程", "淘汰规则", "限流规则"]:
                    if any(kw in tt for tt in texts):
                        keywords_ordered.append(kw)
                theme_label = keywords_ordered[0] if keywords_ordered else (fallback[:12] or "引用的规则")
                # v2.8.69 额外兜底：若抽取后 theme_label 仍只含 规则/的/引用 等虚词残留，
                # 直接回退为「引用未定义规则（多主题）」，避免再次出现 theme_grp_0。
                if (theme_label in ("", "引用的规则", "的规则", "规则")
                        or set(theme_label) <= set("，。：的与保持本次不改动")):
                    theme_label = "引用未定义规则（多主题）"
            else:
                theme_label = raw_k_str
            # v2.8.69: 增强提问的可识别性（按规则类补明确追问维度，
            #   使问题不再是泛化的「请补充定义」，而是像用户说的「某某功能的上传图片规则是什么」）。
            #   规则：若能识别到具体主题 → 生成主题专属名词短语式提问；否则 fallback 给出
            #   明确的「维度清单」让用户知道该补哪几个方面。
            theme_specific_hint = ""
            if any(tk in theme_label for tk in ["图片", "上传", "素材", "文件", "附件"]):
                theme_specific_hint = (
                    "请补充该上传规则：支持的图片/文件格式列表（如 jpg/png/webp）、"
                    "单文件大小限制数值（如 ≤5MB）、超限提示文案或失败处理行为。"
                )
            elif any(tk in theme_label for tk in ["版本", "修订", "改动比例", "小修订", "5%"]):
                theme_specific_hint = (
                    "请补充该版本/小修订判定规则：改动比例的计算方式（按字符数/段落数/编辑距离）、"
                    "阈值数值、边界条件（如图片/表格变更是否计入）。"
                )
            elif any(tk in theme_label for tk in ["权限", "角色", "访问控制"]):
                theme_specific_hint = (
                    "请补充该权限控制规则：涉及的角色列表、各角色允许/禁止的操作、"
                    "特殊权限的申请/审批流程、例外情况。"
                )
            elif any(tk in theme_label for tk in ["保存", "自动保存", "淘汰", "历史版本"]):
                theme_specific_hint = (
                    "请补充该历史版本/自动保存规则：自动保留数量上限、保留时长、"
                    "淘汰优先级、手动保留如何标记。"
                )
            elif "敏感词" in theme_label:
                # v3.0.1: 收紧敏感词专属模板触发条件。原条件含"校验/过滤"过于宽泛，
                # 导致"校验XX规则、沿用现有规则"这类与敏感词无关的主题被错误套用
                # 敏感词专属模板，编造出需求中不存在的"敏感词来源字典/白名单/拦截替换"。
                # 现仅当主题明确含"敏感词"时才使用专属模板，其余走通用维度清单兜底。
                theme_specific_hint = (
                    "请补充该敏感词校验规则：敏感词来源字典、匹配方式（精确/模糊）、"
                    "命中后的处理行为（拦截/提示/替换）、白名单机制。"
                )
            elif "引用未定义规则" in theme_label or theme_label in ("引用的规则",):
                theme_specific_hint = (
                    "请根据实际引用的具体业务规则补充：若涉及数据格式/大小限制，请给出格式清单和阈值数值；"
                    "若涉及流程/状态流转，请给出状态机和边界条件；若涉及角色权限，请给出角色-操作矩阵。"
                )
            else:
                theme_specific_hint = (
                    "请补充：该规则的具体定义（格式/数值/阈值/计算方式/边界条件等细节）。"
                )

            multi_src_note_q = ""
            if len(group) >= 2:
                multi_src_note_q = f"（文档中提及 {len(group)} 处，内容相近需统一定义）"

            src_refs_ctx = "、".join(srcs[:3]) + ("…" if len(srcs) > 3 else "")
            short_texts_str_ctx = "；".join(
                "『" + tt[:80] + ("…』" if len(tt) > 80 else "』") for tt in texts[:2]
            ) + ("；……" if len(texts) > 2 else "")
            multi_src_note_ctx = ""
            if len(group) >= 2:
                multi_src_note_ctx = (
                    "该主题的引用未定义共 " + str(len(group)) + " 处，来源："
                    + "、".join(srcs[:4]) + ("…" if len(srcs) > 4 else "") + "，汇总澄清。"
                )

            # v2.8.70 收敛：已答引用未定义主题不再重复提问
            # 通过 _ref_theme_label / 主题关键词重叠判断，命中已答主题则跳过本轮生成。
            if any(_theme_overlaps(theme_label, _rt) for _rt in resolved_themes):
                logger.info("[ClarifyAgent] 收敛跳过已答引用未定义主题: theme={}", theme_label)
                continue

            questions.append({
                "id": f"ref_undef_{len(questions) + 1}",
                "field": "requirement.ambiguity",
                "priority": "important",
                "category": "ambiguity",
                "question": (
                    f"【{req_title}】引用未定义：「{theme_label}」相关规则未给出具体内容。"
                    f"{multi_src_note_q}{theme_specific_hint}"
                ),
                "context": (
                    f"引用未定义（v2.8.65 规则级兜底）。"
                    f"文档来源：{src_refs_ctx}；相关原文：{short_texts_str_ctx}。"
                    f"表述仅声明『与现有规则一致』/『沿用现有XX』，但通篇未给出"
                    f"具体的数值限制、格式列表或判定标准。若不补充具体内容，"
                    f"SRR中的业务规则和测试用例预期值都将无法确定。{multi_src_note_ctx}"
                ),
                "default_answer": "",
                "input_type": "multi_select",
                "options": [
                    "补充支持的格式列表（如 jpg/png/webp/gif）",
                    "补充单文件大小限制（如 5MB）",
                    "补充超限提示文案或失败处理行为",
                    "补充判定标准/阈值/计算方式（如 5%按字符数）",
                ],
                # v2.8.67: 附加主题标签，供 KB 查询生成时使用（抽短 query，
                # 不用整段长 question 丢进去检索）
                "_ref_theme_label": theme_label,
            })

        # v3.0.1 跨条目矛盾按主题合并：同一主题下的所有冲突位置合并为一条问题
        _MAX_PAIRS_PER_THEME = 8
        for _th, _confs in theme_conflicts.items():
            if not _confs:
                continue
            _total = len(_confs)
            _shown = _confs[:_MAX_PAIRS_PER_THEME]
            _parts = []
            for _c in _shown:
                _parts.append(
                    f"①{_c['si']} 表述为『{_c['side_A']}』/ 原文：{_c['ti'][:120]}；"
                    f"②{_c['sj']} 表述为『{_c['side_B']}』/ 原文：{_c['tj'][:120]}"
                )
            if _total > len(_shown):
                _parts.append(f"……（共 {_total} 处冲突）")
            _summary = f"共{_total}处冲突，" if _total > 1 else ""
            questions.append({
                "id": f"contradiction_{len(questions) + 1}",
                "field": "requirement.logic_consistency",
                "priority": "critical",
                "category": "conflict",
                "question": (
                    f"【{req_title}】检测到跨条目矛盾（{_th}类），{_summary}"
                    f"{'；'.join(_parts)}。"
                    f"以上对同一约束的结论互斥，请统一为一个最终规则。"
                ),
                "context": (
                    f"跨条目语义冲突（v2.8.65 条目级两两对比检测，已按主题合并）。"
                    f"在同一约束主题【{_th}】下存在互相排斥的结论，"
                    f"若不澄清，测试将无法确定版本保留规则的预期（上限 vs 不设上限），"
                    f"生成的 SRR.business_rules / data_constraints 也会互相冲突。"
                ),
                "default_answer": "",
                "input_type": "select",
                "options": [
                    "以更严格的条款为准",
                    "以更宽松的条款为准",
                    "统一为新的中间表述（请用户补充最终规则）",
                ],
            })

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

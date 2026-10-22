from __future__ import annotations

import asyncio
import copy
import difflib
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
from app.agents.clarify_shared import FIELD_PRIORITY, FIELD_QUESTION_TEMPLATES, _build_field_question


# ============================================================================
# 跨来源问题去重辅助（v3.3，修复 N3 澄清问题重复率超标）
# ============================================================================

# 归一化时剥离的"问题外壳"套话——同一缺口被不同链路生成时，
# 差异主要就落在这层外壳上（「文档声明」/「请明确」/「达到上限后应如何处理」…）
_Q_DEDUP_BOILERPLATE: tuple[str, ...] = (
    "需求文档中声明", "文档中声明", "文档声明",
    "但未说明达到该上限后系统的具体处理行为",
    "但未说明达到该上限后系统的处理行为",
    "未说明达到该上限后系统的具体处理行为",
    "未说明达到该上限后系统的处理行为",
    "达到该上限后系统的具体处理行为",
    "达到该上限后系统的处理行为",
    "但未说明达到该上限后的处理行为",
    "请明确：", "请明确:", "请明确",
    "达到上限后应如何处理", "达到上限后如何处理",
    "应如何处理", "如何处理",
    "这些人工操作的执行主体和入口是什么",
    "执行主体和入口是什么",
)

# 归一化时剥离的引号/括号（保留括号内文字，因为里面常是具体阈值）
_Q_DEDUP_QUOTES: tuple[str, ...] = (
    "「", "」", "《", "》", "【", "】", "『", "』",
    "“", "”", "‘", "’", "\"", "'", "（", "）", "(", ")",
)

_Q_DEDUP_PUNCT_RE = re.compile(r"[\s，。、；：？！,.;:?!~—\-/]+")

# 归一化后短句包含判定所需的最小长度（防"必填"这类极短泛化问句吞并他人）
_Q_DEDUP_MIN_CONTAIN_LEN = 4
# 相似度判定的最小长度（过短文本的 difflib 比率不可靠）
_Q_DEDUP_MIN_RATIO_LEN = 12
# 相似度阈值：实测同一缺口跨来源重复时约 0.96~1.0，无关问题通常 < 0.6
_Q_DEDUP_RATIO = 0.90


def _norm_question_for_dedup(text: str) -> str:
    """归一化问题文本，用于跨来源语义去重。

    步骤：去引号/括号 → 去公共套话 → 去空白与标点。
    例：
      「文档声明「单文件大小不超过 10MB」，但未说明达到该上限后系统的处理行为。
        请明确：达到上限后应如何处理？」→ `单文件大小不超过10MB`
      「文档声明「不超过 10MB」，但未说明…请明确：达到上限后应如何处理？」→ `不超过10MB`
    两者归一化后构成包含关系，即可判定为同一缺口。
    """
    t = text or ""
    for q in _Q_DEDUP_QUOTES:
        t = t.replace(q, "")
    for bp in _Q_DEDUP_BOILERPLATE:
        t = t.replace(bp, "")
    return _Q_DEDUP_PUNCT_RE.sub("", t)


def _is_same_gap(a: str, b: str) -> bool:
    """判断两个**已归一化**的问题是否指向同一缺口（保守策略，避免误合并）。"""
    if not a or not b:
        return False
    if a == b:
        return True
    # 包含关系：短句被长句覆盖（要求短句有最小长度，防极短泛化问句吞并）
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= _Q_DEDUP_MIN_CONTAIN_LEN and short in long_:
        return True
    # 近似文本：difflib 比率（对"处置入口"vs"操作入口"这类 1-2 字差异很敏感）
    if len(a) >= _Q_DEDUP_MIN_RATIO_LEN and len(b) >= _Q_DEDUP_MIN_RATIO_LEN:
        if difflib.SequenceMatcher(None, a, b).ratio() >= _Q_DEDUP_RATIO:
            return True
    return False


def _question_keep_rank(q: dict) -> tuple:
    """重复问题保留优先级（越大越优先保留）。

    语义层问句（`llm_semantic.*`）通常带更完整的 context/options，
    比规则层（`prd.*` / `pattern_gap.*`）的短问句更有回答价值，故优先保留。
    """
    field = str(q.get("field", "") or "")
    return (
        1 if field.startswith("llm_semantic.") else 0,
        len(q.get("options") or []),
        len(str(q.get("context", "") or "")),
    )


class ClarifyQuestionGenMixin:
    """澄清问题生成（去重/主题合并/PRD缺口/逻辑矛盾/冷启动）（v3.2.3 从 ClarifyAgent 拆出的 Mixin）。"""

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
    def _dedup_questions_semantic(questions: list[dict], stage: str = "semantic") -> list[dict]:
        """跨来源语义去重（v3.3，修复 N3「澄清问题重复率 42.9% 超标」）。

        为什么需要它
        -----------
        `_dedup_questions` 的去重键是 `field::question全文`，只能抓**完全一致**的重复。
        但同一缺口会被两条检测链路各生成一次：
          - 语义检测：field = `llm_semantic.data_constraints.attachment.size_limit`
          - 规则检测：field = `prd.upper_bound_rule.不超过 10MB`
        **field 与问句前缀都不同**（问句只差「单文件大小」几个字），故被漏判。
        2026-09-11 实测：7 题中 3 组完全重复（重复率 42.9%，阈值 10%）。

        策略（保守，避免误合并）
        ----------------------
        对归一化后的问题文本依次判定：
          ① 完全相同 → 合并
          ② 包含关系（短句被长句覆盖，且短句 ≥ 4 字）→ 合并
          ③ difflib 比率 ≥ 0.90（双方 ≥ 12 字）→ 合并
        冲突时按 `_question_keep_rank` 保留信息量更大的一条（语义层问句优先，
        因其通常带更完整的 context/options）。

        Args:
            questions: 待去重的问题列表
            stage: 日志阶段标识

        Returns:
            list[dict]: 去重后的问题列表（保持原相对顺序）
        """
        if not questions:
            return questions

        kept: list[dict] = []
        normed: list[str] = []
        skipped = 0

        for q in questions:
            n = _norm_question_for_dedup(str(q.get("question", "") or ""))
            dup_idx: int | None = None
            if n:
                for i, kn in enumerate(normed):
                    if _is_same_gap(n, kn):
                        dup_idx = i
                        break
            if dup_idx is None:
                kept.append(q)
                normed.append(n)
                continue

            # 命中重复：保留信息量更大的一条
            skipped += 1
            if _question_keep_rank(q) > _question_keep_rank(kept[dup_idx]):
                logger.info(
                    "[ClarifyAgent] 跨来源语义去重({}) 用更完整的问题替换: "
                    "丢弃 field={!r} 保留 field={!r}",
                    stage, kept[dup_idx].get("field"), q.get("field"),
                )
                kept[dup_idx] = q
                normed[dup_idx] = n
            else:
                logger.info(
                    "[ClarifyAgent] 跨来源语义去重({}) 跳过重复问题: "
                    "保留 field={!r} 丢弃 field={!r} question={!r}",
                    stage, kept[dup_idx].get("field"), q.get("field"),
                    str(q.get("question", ""))[:60],
                )

        if skipped > 0:
            logger.info(
                "[ClarifyAgent] 跨来源语义去重({}) 完成: 原始 {} 条 → 去重后 {} 条（跳过 {} 条）",
                stage, len(questions), len(kept), skipped,
            )
        return kept


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

        # v4.1.2：规则层确定性来源跳过本校验。
        # 本函数针对的是「LLM 精炼阶段 options 与问题 index 错乱」——LLM 才会错位，
        # 规则模板产出的 options 与 question 是**同一处代码成对写死**的，不存在错位可能。
        # 它们的措辞天然不重叠（问句说「菜单/页面入口」，选项说「左侧导航/独立路由」），
        # 反而会被重叠检测误判清空。实测：入口定位题的 5 个候选入口被全清。
        _DETERMINISTIC_SOURCES = ("structure_gap", "prototype_gap", "executability_gap")

        fixed: list[dict] = []
        fixed_count = 0
        for q in questions:
            if str(q.get("_source", "") or "") in _DETERMINISTIC_SOURCES:
                fixed.append(q)
                continue
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
            _sys_prompt = prompt_manager.get("clarify_agent", "SYSTEM_PROMPT")
            result = await self._llm.call_json(
                prompt=user_prompt,
                system_prompt=_sys_prompt,
                schema_hint={"questions": list, "quality_report": dict},
                temperature=0.2,
                agent_name="ClarifyAgent",
            )

            # v3.3（修 N7）：语义层"空结果"检测 + 重试一次。
            #
            # 背景：实测（REQ-1788922967346-1659，连续两次复现）LLM 会返回
            # **散文而非 JSON**（12.9–14.8 万字符，如 "Looking at this requirement…"），
            # `call_json` 的容错路径会返回一个缺 `questions`/`issues_detected` 的结果，
            # 而 `_generate_questions` 不会抛异常 → **语义检测层静默归零**，
            # 澄清降级为纯规则层，但 overall_score 仍高达 0.910，
            # 用户与质量分都察觉不到。语义层本应覆盖的
            # 「跨节矛盾 / 引用未定义 / 信息缺失 / 模糊表述」就此完全失效。
            if not result.get("questions") and not result.get("issues_detected"):
                logger.error(
                    "[ClarifyAgent] 语义检测层返回空结果（questions 与 issues_detected 均缺失），"
                    "重试一次并强制 JSON 输出"
                )
                result = await self._llm.call_json(
                    prompt=(
                        user_prompt
                        + "\n\n【输出格式要求（上次输出不是合法 JSON，请务必遵守）】\n"
                        "只返回一个 JSON 对象，不要任何解释性文字、不要 Markdown 代码块围栏、"
                        "不要在 JSON 前后添加说明。必须以 { 开头、以 } 结尾。"
                    ),
                    system_prompt=_sys_prompt,
                    schema_hint={"questions": list, "quality_report": dict},
                    temperature=0.0,
                    agent_name="ClarifyAgent",
                )

            if not result.get("questions") and not result.get("issues_detected"):
                # 重试后仍为空 → 显式标记降级，避免"高分布 + 空语义层"的假象
                logger.error(
                    "[ClarifyAgent] 语义检测层重试后仍为空 → 本次澄清降级为纯规则层（N7）"
                )
                result["semantic_degraded"] = True

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

        # ------------------------------------------------------------------
        # f) v2.49 可执行性要素缺口（R4 实测驱动）
        # ------------------------------------------------------------------
        # 背景：R4 抽验 754 条用例，25.6% 不可执行、71.8% 需补细节，主因是需求
        # 未提供「可执行性四要素」：入口定位 / 字段响应 / 场景数据 / 故障注入。
        # 生成阶段没有澄清节点（已确认需求 enable_clarify 强制 False，
        # orchestrator.py:701-703），因此只能在需求分析澄清阶段问出来。
        # 生效条件 = 系统级总闸 AND 需求级选项（上传页勾选，默认开）。
        # 需求级由 requirement_tasks 在上传后注入 srr["_clarify_options"]。
        _clarify_opts = srr.get("_clarify_options") or {}
        _req_level_enabled = bool(_clarify_opts.get("executability_check", True))
        if getattr(settings, "CLARIFY_EXECUTABILITY_GAPS_ENABLED", True) and _req_level_enabled:
            self._detect_executability_gaps(raw_text, srr, prd_missing)

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

    @staticmethod
    def _detect_executability_gaps(
        raw_text: str, srr: dict, out: list[dict]
    ) -> None:
        """可执行性要素缺口检测（v2.49，规则层，直接 append 到 out）。

        为什么放在澄清阶段：用例生成阶段**没有澄清节点**（已确认需求强制
        `enable_clarify=False`），所以"生成后才发现的缺口"无法回喂。唯一能在
        现有架构内自动闭环的位置，就是需求分析的澄清——在生成前把
        「可执行性四要素」问清楚。

        四类缺口（与 R4 实测的 B/C 级主因一一对应）：
          1. 入口定位缺失（界面步骤无定位 163 处）
          2. 字段/响应空白（断言目标抽象 157 条）
          3. 场景数据缺失（缺 test_data / 并发基线 103 条）
          4. 故障注入方式缺失（异常类用例无法触发）

        只做规则层保守检测：宁漏不错，避免澄清问题过载。
        """
        def _add(dup_key: str, field: str, desc: str, evidence: str,
                 question: str, options: list[str]) -> None:
            if any(d.get("_dup") == dup_key for d in out):
                return
            out.append({
                "_dup": dup_key,
                "field": field,
                "category": "gap",
                "description": desc,
                "evidence": evidence,
                "suggested_question": question,
                "suggested_options": options,
            })

        # 1) 入口定位缺失：有页面/界面操作描述，但全文无强入口信号
        # 强信号（避免"菜单权限控制""产品可配置"这类通用词误救）：
        #   位置词 + 入口结构的组合描述（左侧导航/顶部菜单/路由地址/页面路径）
        ui_ops = re.search(r'(页面|界面|列表页|表单|弹窗|按钮|下拉|标签页)', raw_text)
        entry_strong = re.search(
            r'(左侧|右侧|顶部|右上角|导航栏|侧边栏|菜单栏|菜单项|'
            r'路由|页面路径|访问路径|URL|(?:从|在).{1,12}(?:菜单|入口|导航).{0,8}进入|'
            r'点击.{0,8}(?:进入|打开).{0,10}(?:页面|界面))',
            raw_text,
        )
        if ui_ops and not entry_strong:
            start = max(0, ui_ops.start() - 40)
            snippet = raw_text[start:ui_ops.end() + 60].replace('\n', ' ')
            _add(
                "exec_ui_entry", "prd.executability.ui_entry",
                "需求描述了页面/界面操作，但未说明入口（菜单位置/页面路径），用例无法定位操作起点",
                f"上下文：…{snippet}…",
                "文档提到了页面/界面操作，但未说明从哪里进入。请补充：该功能位于哪个菜单或页面入口？",
                ["顶部导航菜单", "左侧/侧边栏菜单", "列表页或详情页内的按钮",
                 "独立路由地址", "其他（请说明）"],
            )

        # 2) 字段/响应空白：接口有 path 但 response_body 为空
        empty_resp = [
            str(d.get("path"))
            for d in (srr.get("api_definitions") or [])
            if isinstance(d, dict) and d.get("path")
            and not str(d.get("response_body") or "").strip()
        ]
        if empty_resp:
            _add(
                "exec_api_response", "prd.executability.api_response",
                f"{len(empty_resp)} 个接口未定义响应字段，用例断言无法落到具体字段",
                f"接口：{', '.join(empty_resp[:3])}",
                "上述接口的响应包含哪些字段？请补充关键字段名与含义（用于断言判定）。",
                ["返回单个对象（含 id/status/时间戳）",
                 "返回列表 + 分页（total/items/page）",
                 "仅返回成功标志与提示信息",
                 "其他（请说明）"],
            )

        # 3) 场景数据缺失：涉及并发/多角色/多版本，但无账号或基线数据描述
        scene = re.search(r'(并发|同时操作|多用户|多角色|乐观锁|冲突|版本回滚|多人协作)', raw_text)
        baseline = re.search(
            r'(账号|用户 ?A|用户 ?B|测试数据|基线数据|初始数据|预置数据|角色：|管理员账号|\d+\s*个用户)',
            raw_text,
        )
        if scene and not baseline:
            _add(
                "exec_scene_data", "prd.executability.scene_data",
                "需求涉及并发/多角色/多版本场景，但未提供测试所需的账号与基线数据",
                f"原文提及：{scene.group(0)}",
                "并发/多角色测试需要哪些账号与基线数据？（例如两个不同角色的账号、初始记录数）",
                ["2 个账号（管理员 + 普通用户）",
                 "2 个同角色账号",
                 "3 个及以上不同角色",
                 "其他（请说明）"],
            )

        # 4) 故障注入方式缺失：有降级/熔断/超时等场景，但无强注入信号
        # 强信号（避免"产品可配置"这类通用"配置"误救）：模拟/注入/演练/mock/手动触发
        fault = re.search(r'(降级|熔断|断网|网络中断|超时|失败重试|不可用|宕机|异常恢复)', raw_text)
        inject_strong = re.search(
            r'(模拟|注入|故障演练|手动触发|mock|伪造|拦截返回|打桩|'
            r'开关.{0,8}(?:模拟|注入|强制)|配置项.{0,10}(?:模拟|注入))',
            raw_text, re.IGNORECASE,
        )
        if fault and not inject_strong:
            _add(
                "exec_fault_inject", "prd.executability.fault_inject",
                "需求包含故障/降级场景，但未说明如何模拟该故障，异常类用例无法执行",
                f"原文提及：{fault.group(0)}",
                "该故障场景如何模拟？（决定异常类用例能否执行）",
                ["提供配置开关或开关接口", "停用/断开依赖服务",
                 "注入延迟或错误码", "其他（请说明）"],
            )

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
                    "default_answer": "",  # v4.0.1（S2）：missing_desc 是描述不是答案，不得冒充建议答案
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

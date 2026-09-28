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
from app.agents.clarify_shared import FIELD_PRIORITY, FIELD_QUESTION_TEMPLATES, _build_field_question

class ClarifyDetectMixin:
    """澄清三级检测（完整性/歧义/跨文档矛盾/基线焦点/规则缺口）（v3.2.3 从 ClarifyAgent 拆出的 Mixin）。"""

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
        proto = self._detect_prototype_gaps(srr)
        return cross + proto

    def _detect_prototype_gaps(self, srr: dict) -> list[dict]:
        """原型类来源专属问题（docs/17 §4.4.2，规则层，不新增 LLM 调用）。

        检测到 _raw_text 含「【原型：」标记时，追加原型专属问题模板：
        静态盲区必发问（JS 联动/异步校验原型无法体现）+ 未命名字段汇总。
        与 KB 检索问题合并出题，受现有最大澄清轮次约束。
        """
        import re
        raw_text = srr.get("_raw_text", "") or ""
        if "【原型：" not in raw_text:
            return []

        results: list[dict] = []

        # 1. 静态盲区（必发问）：原型为静态文件，JS 动态行为无法体现
        results.append({
            "type": "prototype_static_blindspot",
            "field": "prototype.dynamic_behavior",
            "description": "原型为静态 HTML，无法体现字段联动显隐、异步校验、页面跳转等 JS 动态行为",
            "evidence": "来源标记: 【原型：",
            "_suggested_question": (
                "原型为静态文件，无法体现动态交互。该页面是否存在字段联动/异步校验/动态显隐/跳转逻辑？请补充说明。"
            ),
            "_suggested_options": [
                "存在字段联动显隐，将补充说明",
                "存在异步校验，将补充说明",
                "存在跳转逻辑，将补充说明",
                "均为静态展示，无动态行为",
            ],
        })

        # 2. 未命名字段汇总 > 0 时发问
        m_unnamed = re.search(r"共 (\d+) 个未命名字段", raw_text)
        if m_unnamed and int(m_unnamed.group(1)) > 0:
            n = m_unnamed.group(1)
            results.append({
                "type": "prototype_unnamed_fields",
                "field": "prototype.unnamed_fields",
                "description": f"原型中有 {n} 个未命名字段（无 label/aria-label/placeholder）",
                "evidence": "解析提示: 未命名字段汇总",
                "_suggested_question": f"原型中有 {n} 个未命名字段，请补充这些字段的业务含义与用途。",
                "_suggested_options": ["将在后续补充说明", "字段可忽略（装饰性元素）"],
            })

        # 3. 删除类按钮：确认交互线索
        m_del = re.search(r'button "([^"]*删[^"]*)"', raw_text)
        if m_del:
            btn = m_del.group(1)
            results.append({
                "type": "prototype_delete_confirm",
                "field": "prototype.delete_confirm",
                "description": f"原型含「{btn}」按钮，未体现二次确认交互",
                "evidence": f"语义树: button \"{btn}\"",
                "_suggested_question": f"「{btn}」类按钮是否需要二次确认（如确认弹窗）？误操作如何撤销？",
                "_suggested_options": [
                    "需要二次确认弹窗",
                    "需要二次确认且有撤销机制",
                    "直接执行，无需确认",
                ],
            })

        # 原型类问题模板设上限（R7：避免打断用户，并入现有澄清轮次约束）
        return results[:3]

    # ========================================================================
    # KB 检索
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
        validated = type(self)._filter_questions_by_necessity(validated)
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

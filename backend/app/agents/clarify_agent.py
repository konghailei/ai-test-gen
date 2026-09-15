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

from app.agents.clarify_shared import FIELD_PRIORITY, FIELD_QUESTION_TEMPLATES, _build_field_question
from app.agents.clarify_question_gen import ClarifyQuestionGenMixin
from app.agents.clarify_detect import ClarifyDetectMixin
from app.agents.clarify_kb import ClarifyKbMixin
from app.agents.clarify_score import ClarifyScoreMixin


class ClarifyAgent(ClarifyQuestionGenMixin, ClarifyDetectMixin, ClarifyKbMixin, ClarifyScoreMixin):
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

    # ========================================================================
    # v3.3（docs/14 二期 A3）：结构缺口驱动澄清
    # ========================================================================
    # 每个结构维度的追问模板。
    # ⚠️ 措辞刻意避开 `_filter_questions_by_necessity` 的"猜想上限类"正则
    # （`超过X怎么办` / `达到上限…`），否则会被当成猜想型问题删掉。
    _STRUCTURE_GAP_TEMPLATES: dict[str, str] = {
        "happy_path_steps": "需求文档未给出主流程步骤。请补充：正常场景下的操作步骤与预期结果。",
        "alternate_flows": "需求文档未描述异常/分支流程。请补充：主流程之外的失败路径或替代流程。",
        "business_rules": "需求文档未包含业务规则。请补充：该功能的判定、计算或约束规则。",
        "non_functional": "需求文档未提出非功能需求。请补充：性能/安全/兼容性/可用性方面的要求。",
        "preconditions": "需求文档未说明前置条件。请补充：在什么前提或状态下才能使用该功能。",
        "data_constraints": "需求文档未说明数据约束。请补充：涉及字段的格式、取值范围或校验规则。",
        "user_roles": "需求文档未明确涉及的用户角色。请补充：哪些角色可以使用或操作该功能。",
        "data_model": "需求文档未描述数据模型。请补充：核心实体、字段及其关系。",
        "title": "需求文档缺少标题。请补充一个能概括本需求范围的标题。",
        "summary": "需求文档缺少摘要。请补充一段说明本需求目标与范围的话。",
    }

    # 取结构缺口时**只取权重 >= 该值**的维度。
    # 依据实测（21 条需求）：`dependencies` 缺失率 86%、`api_definitions` 76%，
    # 属"普遍不适用"而非"普遍漏写"——拿去追问会产生大量伪问题。
    _STRUCTURE_GAP_MIN_WEIGHT: int = 5

    # 结构缺口最多注入几个（避免问题列表被规则项刷屏）
    _STRUCTURE_GAP_MAX: int = 4

    @staticmethod
    async def _load_fp_srrs(req_id: str) -> list[dict]:
        """读取功能点子 SRR。

        为什么需要：实测 `business_rules` / `data_constraints` 在**顶层 SRR 已几乎不存在**
        （1/17、0/17，v2.40 迁移），但在 **FP 子 SRR 中 60/60 全覆盖**。
        只看总 SRR 会把这两个维度恒判缺失。
        """
        if not req_id:
            return []
        try:
            from sqlalchemy import select

            from app.db.database import async_session
            from app.db.models import FunctionalPoint

            async with async_session() as s:
                rows = (await s.execute(
                    select(FunctionalPoint.srr_json).where(
                        FunctionalPoint.requirement_id == req_id
                    )
                )).scalars().all()
            return [r for r in rows if isinstance(r, dict) and r]
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[ClarifyAgent] 读取 FP 子 SRR 失败（结构完整度将仅评总 SRR）: {}",
                str(e)[:120],
            )
            return []

    def _structure_gap_questions(
        self, srr: dict, fp_srrs: list[dict],
    ) -> tuple[list[dict], dict]:
        """把结构完整度的缺口转成澄清问题。

        Returns:
            (问题列表, 结构完整度评估结果)
        """
        from app.utils.requirement_quality import calc_structure_score

        info = calc_structure_score(srr, fp_srrs)
        out: list[dict] = []
        for m in info.get("missing", []):
            if len(out) >= self._STRUCTURE_GAP_MAX:
                break
            if m.get("weight", 0) < self._STRUCTURE_GAP_MIN_WEIGHT:
                continue
            tpl = self._STRUCTURE_GAP_TEMPLATES.get(m["key"])
            if not tpl:
                continue
            out.append({
                "id": f"SG{len(out) + 1:03d}",
                "field": f"structure_gap.{m['key']}",
                "priority": "important" if m.get("weight", 0) >= 8 else "optional",
                "category": "completeness",
                "question": tpl,
                "context": f"结构完整度评估：需求文档未包含「{m['label']}」",
                "default_answer": "",
                "input_type": "text",
                "options": [],
                # 标记来源，便于前端区分"规则缺口"与"LLM 生成"
                "_source": "structure_gap",
            })
        return out, info

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
        # v3.3: 原文案「正在检测需求类型（优化/新建）」已失效——v3.1.6 起需求类型
        # 检测整体移除（见下方 ② 的注释）。该文案误导排查：任务卡在 41% 时看到的是
        # 一个早已不存在的步骤，而实际下一步是规则检测 + LLM 语义检测（可耗时 8 分钟+）。
        await _report("KB 状态检查完成，正在进行规则检测与语义分析...", 41)

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
        # v3.3（docs/14 一期第 3 项）：LLM 输出里本就有 quality_report（见
        # _generate_questions 的 schema_hint），但此前**从未被读取**——等于
        # 自评分支是死代码、规则公式才是实际口径。这里把它捕获下来**仅作观测**
        # （记入 quality_report.llm_self_score，不参与 overall_score 计分），
        # 以便为"是否把 LLM 自评接回加权"积累真实数据后再决策。
        llm_self_report: dict = {}
        # v3.3（N7）：语义检测层是否降级（LLM 输出非 JSON 且重试后仍为空）
        semantic_degraded = False
        try:
            llm_result = await self._generate_questions(
                srr=srr,
                completeness=completeness,
                ambiguities=ambiguities,
                similar_reqs=[],
                augmentations=[],
            )
            _llm_qr = llm_result.get("quality_report") if isinstance(llm_result, dict) else None
            if isinstance(_llm_qr, dict) and isinstance(_llm_qr.get("overall_score"), (int, float)):
                llm_self_report = _llm_qr
                logger.info(
                    "[ClarifyAgent] LLM 自评质量分={}（仅观测记录，不参与门禁计分）",
                    _llm_qr.get("overall_score"),
                )
            # v3.3（N7）：语义层降级标记——LLM 输出非 JSON 且重试后仍为空时置位。
            # 必须向上传递，否则"语义层静默归零 + 质量分仍 0.91"的假象无法被发现。
            if isinstance(llm_result, dict) and llm_result.get("semantic_degraded"):
                semantic_degraded = True
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
            # LLM 语义检测失败不阻断流水线——规则层和 KB 仍可用。
            #
            # v3.3（修 N7）：但**必须显式标记降级**。实测有两种失败模式，
            # 原先两种都只留日志、对外不可见：
            #   模式 A：LLM 返回散文 → 容错路径返回缺 questions 的结果（不抛异常）
            #   模式 B：LLM 返回**格式非法的 JSON**（如第 25 行多余逗号）→ 抛异常走到这里
            # 两者都让语义层归零，而 overall_score 仍可能高达 0.91，
            # 用户与质量分都察觉不到「跨节矛盾/引用未定义/信息缺失/模糊表述」已完全未检测。
            semantic_degraded = True
            logger.error(
                "[ClarifyAgent] LLM 语义检测失败 → 本次澄清降级为纯规则层（N7，模式B）: {}",
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

        # ⑧d v3.3（docs/14 二期 A3）：**结构缺口驱动澄清**
        #
        # 设计：澄清问题池 = 结构缺口（规则，优先）∪ KB 缺口 ∪ LLM 生成。
        # 结构缺口是**确定性**的（不受 LLM 波动影响），且直接对应"需求少写了什么"，
        # 因此作为补充来源注入，让澄清有明确的补全目标，而不是等 LLM 发挥。
        #
        # 注意：本模块只负责生成与合并；结构分的**评分口径**在
        # `app/utils/requirement_quality.py`（已用 21 条真实需求校准）。
        try:
            _fp_srrs = await self._load_fp_srrs(req_id)
            structure_questions, structure_info = self._structure_gap_questions(
                srr, _fp_srrs
            )
            if structure_questions:
                questions.extend(structure_questions)
                logger.info(
                    "[ClarifyAgent] 结构缺口驱动澄清: 注入 {} 个（结构完整度 {}%）缺口={}",
                    len(structure_questions),
                    int(round(structure_info.get("score", 0) * 100)),
                    [q["field"].split(".", 1)[-1] for q in structure_questions],
                )
        except Exception as e:  # noqa: BLE001
            structure_info = {}
            logger.warning(
                "[ClarifyAgent] 结构缺口注入失败（不阻断澄清）: {}", str(e)[:150]
            )

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
        # v3.3: 跨来源语义去重（修 N3）——`_dedup_questions` 只按 field+question 全文去重，
        # 抓不到「同一缺口被语义检测(llm_semantic.*)与规则检测(prd.*)各生成一次」这类
        # field 与问句前缀都不同的重复。2026-09-11 实测重复率 42.9%（阈值 10%）。
        questions = self._dedup_questions_semantic(questions, stage="final")

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

        # 计算质量评分（v3.3：改为返回可解释明细，含前端已声明的
        # completeness_score / ambiguity_score / summary；overall_score 数值口径不变）
        quality_report = self.build_quality_report(
            completeness, ambiguities, {"questions": questions, "quality_report": {}}
        )
        quality_score = float(quality_report.get("overall_score", 0.0))
        if llm_self_report:
            quality_report["llm_self_score"] = round(
                float(llm_self_report.get("overall_score")), 3
            )
            quality_report["llm_self_note"] = (
                "LLM 自评分仅作观测记录，未参与 overall_score 计分（待权重决策）"
            )

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
            "quality_report": quality_report,
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
            # v3.3（N7）：降级标记——语义检测层失效时显式暴露，
            # 避免"质量分很高但语义层什么都没检测"的假象无法被发现。
            "degraded_flags": (["semantic_layer_failed"] if semantic_degraded else []),
            "semantic_degraded": semantic_degraded,
            # v3.3（docs/14 二期 A2/A3/A4）：结构完整度评估结果，
            # 供门禁（A4）与前端展示使用。`missing` 即本次注入的澄清缺口来源。
            "structure_score": structure_info or {},
        }

        if semantic_degraded:
            logger.error(
                "[ClarifyAgent] ⚠️ 本次澄清为**降级结果**：语义检测层未产出任何问题，"
                "仅依赖规则层。overall_score={:.3f} 不代表语义层有效。degraded_flags=['semantic_layer_failed']",
                quality_score,
            )

        logger.info("[ClarifyAgent] ◀ 澄清完成 ▶ 下一节点 QualityGate: status={}, "
                    "overall_score={:.3f}, pass_threshold={}, questions={}, cold_start={}, degraded={}",
                    status, quality_score, threshold, len(questions), cold_start, semantic_degraded)
        return result

    # ========================================================================
    # 答案覆盖评估（v3.0.1 多轮收敛重构）
    # 核心思路：提交答案后不再从零做完整缺口检测，而是「逐条评估上一轮问题
    # 的答案是否已覆盖该问题」。覆盖 → 收敛；未覆盖/模糊 → 追问；用户跳过
    # （未回答）→ 视为不关注，不再追问。这样正常 1 轮正确回答即收敛。
    # ========================================================================


clarify_agent = ClarifyAgent()

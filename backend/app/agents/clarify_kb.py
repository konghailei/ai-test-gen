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

class ClarifyKbMixin:
    """KB 检索与答案核验（检索/答案验证/基线补全/FP 归属/冲突裁决）（v3.2.3 从 ClarifyAgent 拆出的 Mixin）。"""

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
                    "default_answer": "",  # v4.0.1（S2）：missing_desc 是"缺什么"的描述，不是答案，不得冒充建议答案
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
            # v4.1（缺陷 A 修复）：澄清「只用需求内容」，检索面 = 需求权威层
            #  L0 requirement + L1 functional_point + fp_crsr（跨版本聚合权威）。
            #  用例向量在**独立集合**（test_cases），澄清从不检索它；
            #  单值 doc_type="requirement" 会把 L1 层整层排除 → 澄清命中率虚低，
            #  改为多值并集后 L0+L1 同时参与，且天然不含任何测试用例。
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
                        doc_type=["requirement", "functional_point", "fp_crsr"],
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

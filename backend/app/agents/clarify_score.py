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

class ClarifyScoreMixin:
    """澄清质量评分（覆盖评估/评分/嵌套取值）（v3.2.3 从 ClarifyAgent 拆出的 Mixin）。"""

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
                # v3.3: 无待澄清问题 → 视为覆盖充分（1.0 语义成立，非伪造）；
                # 但显式标注 evidence 以便与"真实评估出的 1.0"区分。
                "quality_report": {
                    "overall_score": 1.0,
                    "pass_threshold": self.PASS_THRESHOLD,
                    "coverage": {
                        "answered": 0, "covered": 0, "followup": 0, "skipped": 0,
                        "evidence": "no_questions",
                    },
                    "summary": "无待澄清问题，视为覆盖充分（未经问答评估）",
                },
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
                    # v2.48 修复：此处原为 it["answer"][:300]，会把长答案的后段截掉，
                    # 导致覆盖度评估 LLM 看不到后半部分 → 误判「未覆盖」→ 虚假追问，
                    # 详细答案反而更难收敛（与「一轮收敛」目标相悖）。
                    # 评估必须基于用户答案全文，不截断。
                    "answer": it["answer"],
                })
            try:
                prompt = prompts.ANSWER_COVERAGE_EVAL_PROMPT
                from app.utils.external_content import wrap_external_content
                result = await self._llm.call_json(
                    prompt=prompt.format(
                        raw_text=wrap_external_content((srr.get("_raw_text", "") or "")[:12000]),
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

        # v3.3 修正硬编码评分（docs/14 一期第 4 项）：
        # 原实现 `1.0 if not followup else 0.8` 是常量——恒高于任何警告阈值（0.4），
        # 导致质量警告永不触发、前端 `score >= 0.75` 恒显示「通过」。
        # 改为真实覆盖率：已回答问题中被判定「已覆盖」的占比。
        covered_count = max(0, len(answered) - len(followup))
        coverage_detail: dict = {
            "answered": len(answered),
            "covered": covered_count,
            "followup": len(followup),
            "skipped": len(unanswered_or_skipped),
        }
        coverage_score: float | None
        if answered:
            coverage_detail["evidence"] = "answered"
            coverage_score = round(covered_count / len(answered), 3)
        elif prev_questions:
            # 问题全部被跳过 → 没有任何覆盖证据，**不伪造分数**（原实现返回 1.0）
            coverage_detail["evidence"] = "all_skipped"
            coverage_score = None
        else:
            coverage_detail["evidence"] = "no_questions"
            coverage_score = None

        report: dict = {
            "pass_threshold": self.PASS_THRESHOLD,
            "coverage": coverage_detail,
        }
        # 无证据时不写 overall_score（消费方 `overall_score is not None` 判断已兼容）
        if coverage_score is not None:
            report["overall_score"] = coverage_score
            report["summary"] = (
                f"答案覆盖率 {coverage_score:.2f} = 已覆盖 {covered_count}/已回答 {len(answered)}"
                f"（追问 {len(followup)}、跳过 {len(unanswered_or_skipped)}）"
            )
        else:
            report["summary"] = (
                f"无覆盖证据（{coverage_detail['evidence']}）："
                f"已回答 0、跳过 {len(unanswered_or_skipped)}"
            )
        return {
            "status": status,
            "questions": followup,
            "auto_augmentations": [],
            "quality_report": report,
            "kb_status": kb_status,
            "kb_access_log": [],
            "field_conflicts": [],
        }

    # ========================================================================
    # 完整性检测（docs/01 §4.3.1）
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

    def build_quality_report(
        self,
        completeness: dict,
        ambiguities: list[dict],
        llm_result: dict,
    ) -> dict:
        """构建**可解释**的质量报告（v3.3，docs/14 一期第 3 项）。

        为什么需要它
        ------------
        前端 `client.ts` / `RequirementClarify.tsx` 早已声明
        `quality_report.completeness_score / ambiguity_score / summary`，
        但后端此前只返回 `overall_score` + `pass_threshold`，导致：
          ① 前端声明的三个字段永远为空；
          ② `core/review_reports.py:161` 读 `quality_report["completeness_score"]`
             恒为 None → `requirement_completeness` 维度只能走 blocker 数兜底，
             该维度分实际不反映需求完整性。

        本方法在**不改变 overall_score 数值口径**的前提下补齐明细
        （仍委托 `_calc_quality_score`，故门禁行为零变化），
        使分数可解释、可复核，并为后续「是否重设公式权重」积累落库数据。

        注意：`_calc_quality_score` 保持返回 float 不变（phase3 测试契约依赖），
        本方法是其"明细版"包装，不替代它。
        """
        completeness_score = float(completeness.get("completeness_score", 0) or 0)
        ambiguity_score = min(1.0, len(ambiguities or []) * 0.15)
        llm_report = llm_result.get("quality_report") if isinstance(llm_result, dict) else None
        llm_used = isinstance(llm_report, dict) and "overall_score" in llm_report
        overall = self._calc_quality_score(completeness, ambiguities, llm_result)

        if llm_used:
            summary = (
                f"质量分 {overall:.2f}（来源：LLM 自评；"
                f"规则侧完整性 {completeness_score:.2f}、歧义 {len(ambiguities or [])} 条仅供参考）"
            )
        else:
            summary = (
                f"质量分 {overall:.2f} = 完整性 {completeness_score:.2f}×0.6 "
                f"+ (1−歧义 {ambiguity_score:.2f})×0.4；歧义项 {len(ambiguities or [])} 条"
            )
        return {
            "overall_score": round(overall, 3),
            "pass_threshold": self.PASS_THRESHOLD,
            "completeness_score": round(completeness_score, 3),
            "ambiguity_score": round(ambiguity_score, 3),
            "ambiguity_count": len(ambiguities or []),
            # 可追溯：本分数由哪条路径算出（LLM 自评 / 规则公式）
            "score_source": "llm_self" if llm_used else "rule",
            "summary": summary,
        }

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

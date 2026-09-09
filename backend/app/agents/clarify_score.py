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

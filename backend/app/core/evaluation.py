"""评估运行（evaluation run）：把"提示词能不能上线"从客户端声明改成服务器端证据。

docs/15 §4.5 / P0-5 的问题：
    候选提示词激活只校验客户端提交的 `confirmed_eval: bool` —— 等于自证清白，
    任何调用方都能把未经回归的提示词直接推上生产。

本模块提供三件事：
    ① `score_sample` / `score_dataset`：确定性评分（关键要点命中率），
       不依赖"再调一次 LLM 打分"，因此可复现、可离线测；
    ② `validate_prompt_activation`：纯函数校验（状态 / 目标 / 数据集 / prompt hash）；
    ③ `record_evaluation_run` / `find_evaluation_run`：唯一落库入口与查询。

设计要点：**prompt_hash 逐字校验**。评估跑的是"某一个版本的提示词文本"，
如果候选文本在评估之后被改过，这条评估就不再是对它的证据 —— 必须重新评估。
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from typing import Any

# 评分口径版本：口径变了要升版本，历史评估不会被误当成同一把尺子
PROMPT_EVAL_SCHEMA_VERSION = "prompt-eval-v1"

# 通过阈值：达标样本占比
DEFAULT_PASS_THRESHOLD = 0.8

# 单样本达标判据：必须覆盖要点（must_cover）的命中率下限
KEYPOINT_MIN_RATIO = 0.6


def sha256_text(text: str) -> str:
    """提示词 / 任意文本的内容 hash（激活时逐字校验用）。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def dataset_hash(samples: list[dict]) -> str:
    """数据集 hash：样本与期望都要参与，防止"换了数据集还用旧评估"。"""
    normalized = json.dumps(samples or [], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def gen_eval_run_id() -> str:
    return f"EV-{int(time.time() * 1000)}-{secrets.token_hex(2)}"


def _normalize(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def score_sample(output_text: str, must_cover: list[str]) -> tuple[bool, float, list[str]]:
    """单样本达标判定（纯函数）。

    判据：`must_cover` 中至少 `KEYPOINT_MIN_RATIO` 比例的要点出现在模型输出里。
    用"要点是否提到"而不是"再让 LLM 打分"——后者不可复现，也把评估成本翻倍。

    Returns:
        (是否达标, 命中率, 未命中的要点)
    """
    keys = [k for k in (must_cover or []) if str(k).strip()]
    if not keys:
        # 没有期望要点 = 这条样本无法判定，保守判为不达标（不能白送分）
        return False, 0.0, []
    blob = _normalize(output_text)
    missed = [k for k in keys if _normalize(k) not in blob]
    hit_ratio = (len(keys) - len(missed)) / len(keys)
    return hit_ratio >= KEYPOINT_MIN_RATIO, round(hit_ratio, 3), missed


def score_dataset(results: list[dict]) -> dict:
    """聚合逐样本结果（纯函数）。

    Args:
        results: [{"sample_id": str, "passed": bool, "hit_ratio": float, "missed": [...]}]
    """
    total = len(results or [])
    passed = sum(1 for r in results if r.get("passed"))
    score = round(passed / total, 3) if total else 0.0
    return {
        "sample_count": total,
        "passed_count": passed,
        "score": score,
        "hit_ratio_mean": round(
            sum(float(r.get("hit_ratio", 0.0)) for r in results) / total, 3
        ) if total else 0.0,
    }


def validate_prompt_activation(
    run: dict | None,
    *,
    target: str,
    candidate_prompt: str,
    schema_version: str = PROMPT_EVAL_SCHEMA_VERSION,
) -> tuple[bool, str]:
    """激活前的证据校验（纯函数，便于单测）。

    Returns:
        (是否放行, 拒绝原因)
    """
    if not run:
        return False, (
            "回归门槛未满足：prompt 激活必须引用一条服务器端 passed 的评估运行"
            "（先跑 scripts/eval_prompt_candidate.py，不再接受客户端 confirmed_eval 声明）"
        )
    status = str(run.get("status") or "")
    if status != "passed":
        return False, f"评估运行 {run.get('id')} 状态为 {status or '未知'}，只有 passed 才能作为激活依据"
    if str(run.get("target") or "") != str(target or ""):
        return False, f"评估运行针对的是 {run.get('target')}，与本次激活目标 {target} 不一致"
    if not str(run.get("dataset_hash") or ""):
        return False, f"评估运行 {run.get('id')} 没有数据集 hash，无法证明评的是什么样本"
    if str(run.get("schema_version") or "") != schema_version:
        return False, (
            f"评估运行口径版本为 {run.get('schema_version')}，当前要求 {schema_version}，请重新评估"
        )
    if str(run.get("prompt_hash") or "") != sha256_text(candidate_prompt):
        return False, (
            "评估运行与被激活的候选提示词不一致（候选文本已变更）——"
            "请对新版本重新跑评估，不能用旧评估顶替"
        )
    # 只证明"达标"还不够，必须证明"不比现在更差"（回归语义）
    metrics = run.get("metrics") or {}
    if not metrics.get("baseline_evaluated") and not metrics.get("baseline_unavailable"):
        return False, (
            "评估运行没有基线对比记录（baseline_evaluated/baseline_unavailable 均缺失）——"
            "无法证明候选不劣于当前线上版本；请用默认参数（不要 --no-baseline）重跑评估"
        )
    return True, ""


# ---------------------------------------------------------------------------
# 落库（唯一入口）
# ---------------------------------------------------------------------------

async def record_evaluation_run(
    *,
    kind: str,
    target: str,
    status: str,
    prompt_hash: str,
    dataset_name: str = "",
    dataset_hash_value: str = "",
    metrics: dict | None = None,
    threshold: float = DEFAULT_PASS_THRESHOLD,
    model: str = "",
    provider: str = "",
    schema_version: str = PROMPT_EVAL_SCHEMA_VERSION,
    artifact_uri: str = "",
    notes: str = "",
    created_by: str = "cli",
) -> str:
    """写入一条评估运行记录，返回 run_id。"""
    from app.db.database import async_session
    from app.db.models import EvaluationRun

    metrics = metrics or {}
    run_id = gen_eval_run_id()
    async with async_session() as session:
        session.add(EvaluationRun(
            id=run_id,
            kind=kind,
            target=target,
            status=status,
            dataset_name=dataset_name,
            dataset_hash=dataset_hash_value,
            prompt_hash=prompt_hash,
            sample_count=int(metrics.get("sample_count", 0) or 0),
            passed_count=int(metrics.get("passed_count", 0) or 0),
            threshold=float(threshold),
            score=float(metrics.get("score", 0.0) or 0.0),
            model=model,
            provider=provider,
            schema_version=schema_version,
            metrics=metrics,
            artifact_uri=artifact_uri,
            notes=notes,
            created_by=created_by,
        ))
        await session.commit()
    return run_id


async def find_evaluation_run(run_id: str) -> dict | None:
    """按 ID 读取评估运行（返回普通 dict，便于纯函数校验）。"""
    from sqlalchemy import select

    from app.db.database import async_session
    from app.db.models import EvaluationRun

    async with async_session() as session:
        row = (await session.execute(
            select(EvaluationRun).where(EvaluationRun.id == run_id)
        )).scalar_one_or_none()
        if row is None:
            return None
        return {
            "id": row.id, "kind": row.kind, "target": row.target, "status": row.status,
            "dataset_name": row.dataset_name, "dataset_hash": row.dataset_hash,
            "prompt_hash": row.prompt_hash, "sample_count": row.sample_count,
            "passed_count": row.passed_count, "threshold": row.threshold, "score": row.score,
            "model": row.model, "provider": row.provider,
            "schema_version": row.schema_version, "metrics": row.metrics,
            "artifact_uri": row.artifact_uri, "notes": row.notes,
        }


async def mark_evaluation_run_consumed(run_id: str, integration_id: str) -> None:
    """记录该评估被哪条候选激活消费（可追溯"这条提示词凭什么上线"）。"""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.db.database import async_session
    from app.db.models import EvaluationRun

    async with async_session() as session:
        row = (await session.execute(
            select(EvaluationRun).where(EvaluationRun.id == run_id)
        )).scalar_one_or_none()
        if row is None:
            return
        row.used_for_integration_id = integration_id
        row.used_at = datetime.now(timezone.utc)
        await session.commit()

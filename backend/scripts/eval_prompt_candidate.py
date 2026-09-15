# -*- coding: utf-8 -*-
"""候选提示词评估 CLI（docs/15 §4.5 / P0-5）。

把"提示词能不能上线"从**客户端声明**改成**服务器端证据**：
    评估 → 落 `evaluation_runs`（带 prompt/dataset hash 与逐样本结果）
    → 激活接口必须引用一条 passed 且 prompt_hash 逐字匹配的运行。

用法（容器内执行）：

    # 评估某条候选整合记录（同时评估当前线上版本作为基线对比）
    docker exec ai-test-gen-api python /app/scripts/eval_prompt_candidate.py \
        --integration-id PI-1789...

    # 省额度的冒烟：只跑 1 条样本
    ... --limit 1

    # 只验管道（不调 LLM）：写一条 status=error 记录，**不能**用于激活
    ... --offline

    # 调整门槛
    ... --threshold 0.75 --regression-tolerance 0.10

判据（两条都要满足才算 passed）：
    ① 达标率 >= threshold（默认 0.8）
    ② 不劣于基线：score >= baseline_score - regression_tolerance
       （基线 = 当前线上提示词在同一数据集上的得分；--no-baseline 可跳过，
        但跳过时记录里会标 baseline_evaluated=false，激活侧据此拒绝）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_DATASET = "/app/eval/prompt_candidate_evalset.json"
REGRESSION_TOLERANCE = 0.10


def load_dataset(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data.get("samples"), list) or not data["samples"]:
        raise SystemExit(f"评估集为空或格式不对: {path}")
    return data


async def load_integration(integration_id: str) -> dict:
    from sqlalchemy import select

    from app.db.database import async_session
    from app.db.models import PromptIntegrationRecord

    async with async_session() as session:
        row = (await session.execute(
            select(PromptIntegrationRecord).where(PromptIntegrationRecord.id == integration_id)
        )).scalar_one_or_none()
        if row is None:
            raise SystemExit(f"整合记录不存在: {integration_id}")
        if row.status != "pending":
            raise SystemExit(f"记录状态为 {row.status}，仅 pending 候选需要评估")
        return {
            "id": row.id,
            "target_prompt_id": row.target_prompt_id,
            "candidate_prompt": row.candidate_prompt or "",
        }


async def load_current_prompt(target_prompt_id: str) -> str:
    """取当前线上提示词文本（基线）。取不到则返回空串（调用方降级处理）。"""
    from app.db import prompt_store

    try:
        module, name = target_prompt_id.split(".", 1)
        return prompt_store.get_prompt(module, name) or ""
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 读取当前提示词失败（将跳过基线对比）: {str(e)[:150]}")
        return ""


async def run_samples(prompt_text: str, samples: list[dict], offline: bool) -> list[dict]:
    """在冻结节上跑一条提示词，返回逐样本结果。"""
    from app.core.evaluation import score_sample
    from app.core.llm_client import llm_client

    results: list[dict] = []
    for i, sample in enumerate(samples, 1):
        sid = sample.get("id") or f"sample-{i}"
        if offline:
            output = sample.get("reference_output", "")
            print(f"  [{i}/{len(samples)}] {sid}: offline（用参考输出验管道，不调 LLM）")
        else:
            try:
                output = await llm_client.call(
                    sample["input"],
                    system_prompt=prompt_text,
                    temperature=0.0,
                    max_tokens=int(sample.get("max_tokens", 1200) or 1200),
                    agent_name="prompt_eval",
                )
            except Exception as e:  # noqa: BLE001
                print(f"  [{i}/{len(samples)}] {sid}: ❌ 调用失败 {str(e)[:160]}")
                results.append({"sample_id": sid, "passed": False, "hit_ratio": 0.0,
                                "missed": sample.get("must_cover", []), "error": str(e)[:200]})
                continue
            print(f"  [{i}/{len(samples)}] {sid}: 输出 {len(output or '')} 字")
        passed, hit_ratio, missed = score_sample(output or "", sample.get("must_cover") or [])
        flag = "✅" if passed else "❌"
        print(f"      {flag} 要点命中率={hit_ratio} 未命中={missed}")
        results.append({"sample_id": sid, "passed": passed, "hit_ratio": hit_ratio,
                        "missed": missed, "output_excerpt": (output or "")[:400]})
    return results


async def main() -> int:
    ap = argparse.ArgumentParser(description="候选提示词评估（P0-5）")
    ap.add_argument("--integration-id", required=True, help="候选整合记录 ID（PI-...）")
    ap.add_argument("--dataset", default=DEFAULT_DATASET, help="冻结评估集路径")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条样本（0=全部）")
    ap.add_argument("--threshold", type=float, default=0.8, help="达标率阈值")
    ap.add_argument("--regression-tolerance", type=float, default=REGRESSION_TOLERANCE,
                    help="允许相对基线的下降幅度")
    ap.add_argument("--no-baseline", action="store_true", help="跳过基线对比（记录会标记，激活侧会拒绝）")
    ap.add_argument("--offline", action="store_true",
                    help="不调 LLM，仅验管道（写 status=error，不能用于激活）")
    args = ap.parse_args()

    from app.api.settings import load_db_config_to_settings
    from app.core.evaluation import (
        PROMPT_EVAL_SCHEMA_VERSION,
        dataset_hash,
        record_evaluation_run,
        score_dataset,
        sha256_text,
    )
    from app.db.database import async_session

    # 独立进程不走 lifespan：先加载 DB 里的 LLM 配置，否则报"无可用 LLM 提供商"
    if not args.offline:
        async with async_session() as session:
            await load_db_config_to_settings(session)

    dataset = load_dataset(args.dataset)
    samples = dataset["samples"]
    if args.limit > 0:
        samples = samples[: args.limit]
    record = await load_integration(args.integration_id)
    target = record["target_prompt_id"]
    candidate = record["candidate_prompt"]
    if not candidate.strip():
        raise SystemExit("候选提示词为空，无法评估")

    module = target.split(".", 1)[0]
    hint = str(dataset.get("target_hint") or "")
    if hint and hint not in module:
        print(f"⚠️ 评估集 target_hint={hint} 与目标模块 {module} 不一致，结果仅供参考")

    print("=" * 68)
    print(f"候选提示词评估: {record['id']}  目标={target}")
    print(f"数据集: {dataset.get('name')}  样本={len(samples)}  阈值={args.threshold}")
    print(f"候选 prompt_hash: {sha256_text(candidate)[:16]}…")
    print("=" * 68)

    print("\n[候选] 评估中…")
    candidate_results = await run_samples(candidate, samples, args.offline)
    cand_metrics = score_dataset(candidate_results)

    baseline_evaluated = False
    baseline_unavailable = False
    baseline_unavailable_reason = ""
    baseline_score = None
    baseline_prompt = ""
    if args.no_baseline:
        baseline_unavailable = True
        baseline_unavailable_reason = "调用方显式 --no-baseline"
    elif args.offline:
        baseline_unavailable = True
        baseline_unavailable_reason = "offline 冒烟不做基线"
    else:
        baseline_prompt = await load_current_prompt(target)
        if not baseline_prompt.strip():
            baseline_unavailable = True
            baseline_unavailable_reason = "取不到当前线上提示词文本"
        elif sha256_text(baseline_prompt) == sha256_text(candidate):
            baseline_unavailable = True
            baseline_unavailable_reason = "候选与当前线上版本逐字相同"
        else:
            print("\n[基线] 当前线上版本评估中…")
            baseline_results = await run_samples(baseline_prompt, samples, offline=False)
            baseline_score = score_dataset(baseline_results)["score"]
            baseline_evaluated = True
            print(f"  基线得分={baseline_score}")

    no_regression = True
    if baseline_evaluated and baseline_score is not None:
        no_regression = cand_metrics["score"] >= baseline_score - args.regression_tolerance

    if args.offline:
        status = "error"
        notes = "offline 冒烟：未真实调用模型，不作为激活依据"
    elif cand_metrics["score"] >= args.threshold and no_regression:
        status = "passed"
        notes = ""
    else:
        status = "failed"
        reasons = []
        if cand_metrics["score"] < args.threshold:
            reasons.append(f"达标率 {cand_metrics['score']} < 阈值 {args.threshold}")
        if not no_regression:
            reasons.append(
                f"相对基线回退（{cand_metrics['score']} < {baseline_score} - {args.regression_tolerance}）"
            )
        notes = "；".join(reasons) or "未达标"

    metrics = {
        **cand_metrics,
        "baseline_evaluated": baseline_evaluated,
        "baseline_unavailable": baseline_unavailable,
        "baseline_unavailable_reason": baseline_unavailable_reason,
        "baseline_score": baseline_score,
        "regression_tolerance": args.regression_tolerance,
        "baseline_prompt_hash": sha256_text(baseline_prompt) if baseline_prompt else "",
        "samples": candidate_results,
        "dataset_name": dataset.get("name", ""),
    }

    run_id = await record_evaluation_run(
        kind="prompt_candidate",
        target=target,
        status=status,
        prompt_hash=sha256_text(candidate),
        dataset_name=str(dataset.get("name", "")),
        dataset_hash_value=dataset_hash(dataset),
        metrics=metrics,
        threshold=args.threshold,
        model=os.environ.get("LLM_MODEL", ""),
        provider=os.environ.get("LLM_PROVIDER", ""),
        schema_version=PROMPT_EVAL_SCHEMA_VERSION,
        artifact_uri=args.dataset,
        notes=notes,
        created_by="cli",
    )

    print("\n" + "=" * 68)
    print(f"状态={status}  候选得分={cand_metrics['score']}  "
          f"通过样本={cand_metrics['passed_count']}/{cand_metrics['sample_count']}"
          + (f"  基线得分={baseline_score}" if baseline_evaluated else "  （未做基线对比）"))
    print(f"evaluation_run_id = {run_id}")
    if status == "passed":
        print("→ 激活：POST /api/review-reports/prompt-candidates/"
              f"{record['id']}/activate  {{\"evaluation_run_id\": \"{run_id}\"}}")
    else:
        print(f"→ 不可激活：{notes}")
    print("=" * 68)
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

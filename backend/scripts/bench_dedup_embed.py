"""分片并行 embed 基准脚本（docs/27 分片并行 · 服务器部署验证用）。

用途：在内网服务器部署后运行本脚本，实测当前硬件形态下
DEDUP_EMBED_WORKERS 各档位的真实加速比，给出推荐配置。
   docker exec ai-test-gen-api python /app/scripts/bench_dedup_embed.py
可选参数：
   --n 96          测试文本条数（默认 96，约 6-8min 全档跑完）
   --workers 2,4   测哪些并行档（默认测 1/2/4/auto）
   --model PATH    覆盖模型路径（默认取 settings 或 compose 挂载路径）

dev 机实测（2026-10-12，8 逻辑核 / 16GB 内存）：串行 127s / 2w 线程池 122s
（1.04× 假并行）/ 4w 线程池 333s（负优化）；ProcessPool 因内存不足无法
完成基准。结论：dev 不开并行；服务器多物理核 + 内存充裕时 ProcessPool
分片预期近线性加速，但**必须在本机实测确认**——本脚本即为此而写。

判定口径：加速比 < 1.3× 的档位不值得开（进程池有固定开销）。
"""
from __future__ import annotations

import argparse
import json
import os
import time

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def _default_model_path() -> str:
    try:
        from app.config import settings

        for name in (settings.DEDUP_EMBEDDING_MODEL_NAME, settings.EMBEDDING_MODEL_NAME):
            if name and os.path.isabs(name) and os.path.isdir(name):
                return name
    except Exception:  # noqa: BLE001
        pass
    for p in ("/models/bge-large-zh-v1.5",):
        if os.path.isdir(p):
            return p
    return ""


def _make_texts(n: int) -> list[str]:
    """构造 n 条真实用例量级的整例 JSON 文本（与 _semantic_text 口径一致）。"""
    base = {
        "title": "验证登录功能在连续失败5次后锁定账户30分钟",
        "type": "functional",
        "description": "用户在登录界面连续输入错误密码5次后，系统应锁定该账户30分钟并提示锁定信息，锁定期间即使输入正确密码也无法登录",
        "preconditions": ["用户账户已注册且状态正常", "系统锁定策略已配置为5次失败锁定30分钟"],
        "steps": ["打开登录页面", "输入已注册的用户名", "输入错误密码并点击登录",
                  "重复输入错误密码登录共5次", "第6次尝试输入正确密码登录"],
        "assertions": ["前4次错误登录提示密码错误", "第5次错误登录后提示账户已锁定",
                       "锁定期间正确密码也无法登录", "锁定30分钟后可正常登录"],
        "tags": ["登录", "安全", "锁定"],
        "notes": "覆盖: 登录锁定; 技术: 边界值",
    }
    out = []
    for i in range(n):
        c = json.loads(json.dumps(base))
        c["title"] = f"验证登录功能在连续失败{5 + i % 3}次后锁定账户{30 + i % 2}分钟"
        c["steps"] = [f"[场景{i % 4}] {s}" for s in c["steps"]]
        out.append(json.dumps(c, ensure_ascii=False))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=96)
    ap.add_argument("--workers", type=str, default="1,2,4,auto")
    ap.add_argument("--model", type=str, default="")
    args = ap.parse_args()

    model_path = args.model or _default_model_path()
    if not model_path:
        print("!! 未找到模型路径，用 --model 指定（绝对路径）")
        raise SystemExit(2)
    print(f"model={model_path}  n={args.n}  workers={args.workers}")
    print(f"cpu_count(logical)={os.cpu_count()}  mem_available_gb="
          f"{os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_AVPHYS_PAGES') / 1e9:.1f}")

    from concurrent.futures import ProcessPoolExecutor

    from app.db.milvus_store import (
        _dedup_encode_chunk,
        _dedup_encode_worker_init,
        _physical_cores,
    )
    from sentence_transformers import SentenceTransformer

    texts = _make_texts(args.n)

    def run_serial() -> list[list[float]]:
        import torch

        torch.set_num_threads(4)  # 与 worker 档同粒度，保证可比
        m = SentenceTransformer(model_path, device="cpu")
        return m.encode(texts, normalize_embeddings=True, show_progress_bar=False).tolist()

    def run_parallel(nw: int) -> list[list[float]]:
        chunks = [texts[i::nw] for i in range(nw)]
        with ProcessPoolExecutor(
            max_workers=nw, initializer=_dedup_encode_worker_init,
        ) as ex:
            futs = [ex.submit(_dedup_encode_chunk, (c, model_path))
                    for c in chunks if c]
            out: list[list[float]] = []
            for f in futs:
                out.extend(f.result())
        return out

    t0 = time.time()
    base = run_serial()
    t_serial = time.time() - t0
    print(f"\n[workers=1 serial] {t_serial:.1f}s  ({t_serial / args.n:.2f}s/条)")

    results: dict[str, float] = {"1": t_serial}
    for w in [x.strip() for x in args.workers.split(",") if x.strip()]:
        if w in ("1", ""):
            continue
        nw = max(1, _physical_cores() - 1) if w == "auto" else int(w)
        if nw <= 1:
            continue
        try:
            t0 = time.time()
            vecs = run_parallel(nw)
            dt = time.time() - t0
            results[str(nw)] = dt
            print(f"[workers={nw}] {dt:.1f}s  加速比 {t_serial / dt:.2f}×")
            # 一致性抽查：与串行逐位近似（防"快但算错"）
            import math

            diff = max(abs(a - b) for ra, rb in zip(vecs, base)
                       for a, b in zip(ra, rb)) if vecs else 0.0
            flag = "OK" if diff < 1e-3 else f"DIVERGED({diff:.2e})"
            print(f"            与串行最大向量差 {diff:.2e} [{flag}]")
        except Exception as e:  # noqa: BLE001
            print(f"[workers={nw}] 失败（回退串行）: {str(e)[:150]}")

    best = max(results, key=lambda k: t_serial / results[k]) if len(results) > 1 else "1"
    speedup = t_serial / results[best]
    print(f"\n=== 结论 ===")
    print(f"最优档位: workers={best}（加速 {speedup:.2f}×）")
    if speedup < 1.3:
        print("建议: 本机并行收益 <1.3×，DEDUP_EMBED_WORKERS 留空（串行）。")
    else:
        print(f"建议: 设置 DEDUP_EMBED_WORKERS={best}"
              f"（内存预算 ≈ {int(best) * 2}GB，确认空闲内存充足）")


if __name__ == "__main__":
    main()

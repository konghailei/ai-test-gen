# -*- coding: utf-8 -*-
"""评估统计 CLI（P2-3）。

用法（在 ai-test-gen-api 容器内执行）：
  # 两个配置的配对比较（各自 jsonl，每行 {"case_id":..., "passed": true/false}）
  python /app/scripts/eval_stats.py pair /app/eval/run_a.jsonl /app/eval/run_b.jsonl

  # 稳定性口径
  python /app/scripts/eval_stats.py ptrials --n 10 --c 6 --k 5
"""
import argparse
import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_results(path: str) -> dict[str, bool]:
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            out[rec["case_id"]] = bool(rec["passed"])
    return out


def cmd_pair(args):
    from app.core.eval_stats import mcnemar_exact, wilson_ci
    a = load_results(args.run_a)
    b = load_results(args.run_b)
    common = sorted(set(a) & set(b))
    if not common:
        print("两个结果文件没有共同 case_id，无法配对")
        sys.exit(2)
    both_pass = sum(1 for cid in common if a[cid] and b[cid])
    b_only = sum(1 for cid in common if a[cid] and not b[cid])   # A 好 B 差
    c_only = sum(1 for cid in common if b[cid] and not a[cid])   # B 好 A 差
    both_fail = len(common) - both_pass - b_only - c_only
    p = mcnemar_exact(b_only, c_only)
    ra, rb = wilson_ci(sum(1 for cid in common if a[cid]), len(common)), wilson_ci(
        sum(1 for cid in common if b[cid]), len(common)
    )
    print(f"配对用例数: {len(common)}")
    print(f"  双通过: {both_pass}  双失败: {both_fail}")
    print(f"  仅 A 通过: {b_only}  仅 B 通过: {c_only}")
    print(f"  A 成功率: {sum(1 for cid in common if a[cid])/len(common):.3f}  CI={ra[0]:.3f}~{ra[1]:.3f}")
    print(f"  B 成功率: {sum(1 for cid in common if b[cid])/len(common):.3f}  CI={rb[0]:.3f}~{rb[1]:.3f}")
    print(f"  McNemar 精确 p 值: {p:.4f}")
    if p < 0.05:
        winner = "A" if b_only > c_only else "B"
        print(f"  ⇒ 差异显著（p<0.05），{winner} 更优")
    else:
        print("  ⇒ 差异不显著（p>=0.05），分差小于噪声带宽，不应据此切换配置")


def cmd_ptrials(args):
    from app.core.eval_stats import pass_at_k, pass_hat_k
    pk = pass_at_k(args.n, args.c, args.k)
    phk = pass_hat_k(args.n, args.c, args.k)
    print(f"n={args.n} c={args.c} k={args.k}")
    print(f"  Pass@{args.k}  = {pk:.3f}  （k 次内至少一次成功——探索口径）")
    print(f"  Pass^{args.k} = {phk:.3f}  （k 次全部成功——回归/稳定口径）")


def main():
    ap = argparse.ArgumentParser(description="评估统计口径工具（Pass@k/Pass^k/McNemar）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("pair", help="两个配置的配对显著性比较")
    p1.add_argument("run_a")
    p1.add_argument("run_b")
    p2 = sub.add_parser("ptrials", help="单配置的 Pass@k / Pass^k")
    p2.add_argument("--n", type=int, required=True, help="总采样次数")
    p2.add_argument("--c", type=int, required=True, help="成功次数")
    p2.add_argument("--k", type=int, required=True, help="抽取次数")
    args = ap.parse_args()
    if args.cmd == "pair":
        cmd_pair(args)
    else:
        cmd_ptrials(args)


if __name__ == "__main__":
    main()

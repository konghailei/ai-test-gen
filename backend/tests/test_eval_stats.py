# -*- coding: utf-8 -*-
"""eval_stats 单元测试（P2-3）。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.eval_stats import pass_at_k, pass_hat_k, mcnemar_exact, wilson_ci, cohens_kappa

PASS, FAIL = [], []
def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name)

# pass@k / pass^k：书 6.4 的数字（单次 60% → Pass@5≈99%，Pass^5≈7.8%）
# n=100, c=60, k=5
check("pass@5(60%)≈0.99", abs(pass_at_k(100, 60, 5) - 0.99) < 0.005)
v5 = pass_hat_k(100, 60, 5)
check("pass^5(60%) 在 0.07~0.085", 0.07 < v5 < 0.085)
check("pass@1 == 单次成功率", abs(pass_at_k(100, 60, 1) - 0.60) < 1e-9)
check("pass^1 == 单次成功率", abs(pass_hat_k(100, 60, 1) - 0.60) < 1e-9)
check("全成功 pass@k=1", pass_at_k(10, 10, 3) == 1.0)
check("全失败 pass^k=0", pass_hat_k(10, 0, 3) == 0.0)

# mcnemar：b=c 对称 → p=1；极端不对称 → 显著
check("mcnemar(5,5)=1.0", mcnemar_exact(5, 5) == 1.0)
check("mcnemar(0,0)=1.0", mcnemar_exact(0, 0) == 1.0)
check("mcnemar(10,1)<0.02", mcnemar_exact(10, 1) < 0.02)
check("mcnemar(15,2)<0.005 (精确值≈0.00235)", mcnemar_exact(15, 2) < 0.005)
check("mcnemar 对称性", abs(mcnemar_exact(10, 1) - mcnemar_exact(1, 10)) < 1e-12)

# wilson：n=100 p=0.7 → 区间约 [0.60, 0.78]（书 6.7 的 ±9% 口径）
lo, hi = wilson_ci(70, 100)
check("wilson(70/100) 下界≈0.60", abs(lo - 0.604) < 0.02)
check("wilson(70/100) 上界≈0.78", abs(hi - 0.784) < 0.02)

# kappa：完全一致=1；与随机一致=0
check("kappa 完全一致=1", cohens_kappa(["a","b","a","b"], ["a","b","a","b"]) == 1.0)
k0 = cohens_kappa(["a","a","b","b"], ["b","b","a","a"])
check("kappa 完全反相关=-1", abs(k0 + 1) < 1e-9)
k1 = cohens_kappa(["a"]*50 + ["b"]*50, ["a"]*45 + ["b"]*55)
check("kappa 0<k1<1", 0 < k1 < 1)

print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL); sys.exit(1)
print("ALL_OK")

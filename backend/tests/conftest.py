"""pytest 收集策略与测试分层（docs/15 §4.5 / P0-4）。

`backend/tests/` 混着两类文件，CI 不能用同一种方式对待：

① **pytest 风格单元用例**（无外部依赖）—— unit 层，CI 必须通过，失败即红灯；
② **脚本式集成用例**（`test_phaseN_*.py`、`test_e2e_*.py` 等，自带 `main()`，
   直接 `python tests/xxx.py` 运行，依赖 PG / Redis / Milvus）—— 默认**不收集**，
   否则"没有服务"会把 unit 门禁染红（原 CI 用 `continue-on-error` 掩盖了这一点，
   代价是 pytest 失败根本不阻断）。

启用集成层（本地或 CI 的 service-container job）：

    AITG_RUN_INTEGRATION=1 python -m pytest backend/tests -q
"""

from __future__ import annotations

import os

import pytest

RUN_INTEGRATION = os.environ.get("AITG_RUN_INTEGRATION") == "1"

# 脚本式用例：自带 main()，用 python 直接跑；且都需要真实服务
SCRIPT_SUITES = [
    "test_phase2_core.py",
    "test_phase3_agents.py",
    "test_phase4_orchestrator.py",
    "test_phase5_api.py",
    "test_e2e_real_api.py",
    "test_eval_stats.py",
    "test_xmind_export.py",
    "test_v2_55_kb_optimization.py",
    "test_v2_56_fp_gate.py",
    "test_v2_56_kqa_review.py",
]

# 未启用集成层时，这些文件不参与收集（unit 门禁只跑无外部依赖的用例）
collect_ignore = [] if RUN_INTEGRATION else list(SCRIPT_SUITES)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "integration: 需要真实服务（PG / Redis / Milvus）的用例"
    )


@pytest.fixture
def client():
    """FastAPI TestClient。

    unit 层默认 skip（需要真实 PG/Redis/Milvus），集成层通过
    `AITG_RUN_INTEGRATION=1` 启用 —— 避免"没有服务"被记成 pytest ERROR。
    """
    if not RUN_INTEGRATION:
        pytest.skip("integration：需要真实服务，设置 AITG_RUN_INTEGRATION=1 启用")
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c

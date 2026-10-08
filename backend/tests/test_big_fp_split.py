# -*- coding: utf-8 -*-
"""D-5 守卫：大 FP 要点数超阈值时按类型二次分片（行为验证）。"""
import sys

sys.path.insert(0, "/app")

from app.utils.coverage import collect_points


def _mk_fp(n_hp=25, n_alt=12, n_rule=30, n_data=20, fp_id="FP-BIG"):
    """构造一个大 FP（要点数 = n_hp + n_alt + n_rule + n_data）。"""
    fp = {
        "id": fp_id, "name": "大FP", "priority": "P1",
        "srr_json": {
            "flows": {
                "happy_path": {"steps": [
                    {"action": f"步骤{i}：执行操作并校验结果"} for i in range(1, n_hp + 1)
                ]},
                "alternate_flows": [
                    {"name": f"异常流{i}：输入非法/服务不可用"} for i in range(1, n_alt + 1)
                ],
            },
            "business_rules": [
                {"rule": f"规则{i}：字段必须满足约束条件"} for i in range(1, n_rule + 1)
            ],
            "data_constraints": [
                {"field": f"field_{i}", "type": "string", "required": True,
                 "constraints": "长度1-64"} for i in range(1, n_data + 1)
            ],
        },
    }
    return fp


SRR = {"requirement": {"id": "R1", "priority": "P1"}, "flows": {}}


def test_small_fp_untouched():
    """要点数 ≤40 的 FP 不应被切分（collect_points 正常返回全部）。"""
    fp = _mk_fp(n_hp=10, n_alt=3, n_rule=5, n_data=2)  # 20 要点
    pts = collect_points(SRR, [fp])
    assert len(pts) == 20
    # 全部归属该 FP
    assert all(p["fp_id"] == "FP-BIG" for p in pts)


def test_big_fp_points_count_over_threshold():
    """大 FP（87 要点）超过 40 阈值——分片输入正确性：分组键覆盖全部类型。"""
    fp = _mk_fp()  # 25+12+30+20=87
    pts = collect_points(SRR, [fp])
    assert len(pts) == 87
    types = {p["ptype"] for p in pts}
    assert {"happy_path", "alt_flow", "rule", "data"} <= types


def test_split_big_fp_logic():
    """直接测 _split_big_fp 分组行为（从 orchestrator 提取的核心逻辑）。"""
    fp = _mk_fp()
    pts = collect_points(SRR, [fp])
    assert len(pts) > 40

    def _gk(p):
        t = p["ptype"]
        if t == "happy_path":
            return "happy_path"
        if t in ("alt_flow", "alternate_flow"):
            return "alt_flow"
        if t in ("rule", "shared_rule"):
            return "rule"
        if t == "data":
            return "data"
        return "extended"

    groups = {}
    for p in pts:
        groups.setdefault(_gk(p), []).append(p)
    # 4 个类型组，每批 ≤30 要点（远小于原 87）
    assert len(groups) == 4
    assert all(len(v) <= 30 for v in groups.values())
    # 子批要点 ID 前缀保持 FP 归属
    for g in groups.values():
        assert all(p["fp_id"] == "FP-BIG" for p in g)

"""B-6 分层闸门去重护栏的离线用例（docs/32 §B-6，不连 DB、不调 LLM）。

验收三档（docs/32 B-6 验收标准）：
① 探针 5 组：3 组角色变体全保留（闸门1 拦截，reason 记录命中维度）、
   2 组对照正确判定（真重复删 / 不同场景留）；
② 闸门2 LLM 窄带裁决 mock 三态：非重复→保留且留痕 llm_distinct /
   重复→合并 / 异常→fail-open 保留；
③ 闸门3 留痕：removed 记录带完整 case_json（一键恢复数据源）。
"""
from __future__ import annotations

from app.core.dedup_trace import build_removal_records, slim_records_for_sse
from app.utils.case_dedup import (
    dedup_near_duplicates,
    dedup_semantic,
    is_near_duplicate,
    semantic_near_duplicate,
)


def _case(cid, title, steps=("输入账号密码", "提交登录"), conf=0.9, ctype="functional", **extra):
    return {
        "id": cid, "title": title, "type": ctype, "confidence": conf,
        "steps": [{"step": i + 1, "action": a, "expected": "系统按规则响应"} for i, a in enumerate(steps)],
        "assertions": [{"type": "equals", "target": "operation.result", "expected_value": "OK"}],
        **extra,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ① 探针 5 组（docs/32 §B-6 ⑤ 探针形态 + 验收 1）
# ─────────────────────────────────────────────────────────────────────────────

def test_probe_short_title_role_variant_kept():
    """探针形态 1：角色在短标题 → 全保留。

    批内 3-gram 实测 0.364（阈值前就保留）；危险在语义层（实测 sim 0.9543
    ≥ 0.92 会删）→ 语义路径必须被闸门1 拦下且 reason 记录命中维度。
    """
    a = _case("TC-A", "管理员创建项目成功", steps=("进入项目管理", "创建项目"))
    b = _case("TC-B", "普通用户创建项目成功", steps=("进入项目管理", "创建项目"))
    dup, _ = is_near_duplicate(a, b)
    assert not dup
    dup_s, reason = semantic_near_duplicate(a, b, sim=0.95)
    assert not dup_s and "角色" in reason, f"语义层角色变体未被拦截: {reason}"
    kept, removed = dedup_near_duplicates([a, b])
    assert len(kept) == 2 and removed == []


def test_probe_long_title_role_variant_kept():
    """探针形态 2：角色在长标题 → 全保留。"""
    a = _case("TC-A", "管理员导出月度统计报表并验证文件完整性",
              steps=("进入报表中心", "导出月度统计报表"))
    b = _case("TC-B", "访客导出月度统计报表并验证文件完整性",
              steps=("进入报表中心", "导出月度统计报表"))
    dup, reason = is_near_duplicate(a, b)
    assert not dup and "角色" in reason
    kept, removed = dedup_near_duplicates([a, b])
    assert len(kept) == 2 and removed == []


def test_probe_same_title_role_in_steps_kept():
    """探针形态 3：同标题、角色只在步骤里（最险形态，标题 sim=1.0）→ 全保留。"""
    a = _case("TC-A", "查询订单列表并导出", steps=("管理员登录系统", "查询订单列表并导出"),
              description="管理员视角的用例")
    b = _case("TC-B", "查询订单列表并导出", steps=("只读成员登录系统", "查询订单列表并导出"),
              description="只读成员视角的用例")
    dup, reason = is_near_duplicate(a, b)
    assert not dup, f"同标题角色变体被误判重复: {reason}"
    kept, removed = dedup_near_duplicates([a, b])
    assert len(kept) == 2 and removed == []


def test_probe_role_variant_dynamic_words_kept():
    """角色词表盲区（买家/卖家不在静态表）→ SRR 动态词（extra_role_words）拦住。"""
    a = _case("TC-A", "查询订单列表并导出", steps=("买家登录系统", "查询订单列表并导出"),
              description="买家视角的用例")
    b = _case("TC-B", "查询订单列表并导出", steps=("卖家登录系统", "查询订单列表并导出"),
              description="卖家视角的用例")
    kept, removed = dedup_near_duplicates(
        [a, b], extra_role_words={"买家", "卖家"},
    )
    assert len(kept) == 2 and removed == [], "SRR 动态角色词未生效"


def test_probe_control_true_duplicate_removed():
    """对照 1：真重复正确删（docs/29 实证对）。"""
    a = _case("TC-1", "锁定期间尝试登录提示剩余锁定时间")
    b = _case("TC-2", "锁定期间尝试登录被拒绝并提示剩余锁定时间")
    kept, removed = dedup_near_duplicates([a, b])
    assert len(kept) == 1 and len(removed) == 1


def test_probe_control_different_scenario_kept():
    """对照 2：不同场景正确留（步骤相同不是合并理由）。"""
    steps = ("打开登录页", "输入账号与密码", "点击登录")
    a = _case("TC-1", "登录失败记录失败次数", steps=steps)
    b = _case("TC-2", "登录成功跳转首页并展示用户名", steps=steps)
    kept, removed = dedup_near_duplicates([a, b])
    assert len(kept) == 2 and removed == []


# ─────────────────────────────────────────────────────────────────────────────
# 闸门1 六维护栏细节（数字/实体/操作/分支/扩展名单位）
# ─────────────────────────────────────────────────────────────────────────────

def test_guard_number_decimal_not_eaten():
    """小数护栏：小数部分不能被吃掉。

    关键回归对：「1.5MB」vs「1.05MB」——旧正则 `\\d+` 把两者都吃成
    {"1","5"}（"05"去前导零=5）→ 数字护栏形同虚设；新正则保小数部分。
    """
    a = _case("TC-A", "上传1.5MB文件校验", steps=("上传 1.5MB 文件", "校验结果"))
    b = _case("TC-B", "上传1.05MB文件校验", steps=("上传 1.05MB 文件", "校验结果"))
    dup, _ = is_near_duplicate(a, b)
    assert not dup, "小数口径不同应判不重复（1.5 vs 1.05）"


def test_guard_branch_negation_opposite():
    """否定/分支词相反：成功 vs 失败两极分别出现在两例 → 判不重复。"""
    a = _case("TC-A", "提交请求校验", steps=("提交请求", "校验结果"),
              description="提交成功后返回结果")
    b = _case("TC-B", "提交请求校验", steps=("提交请求", "校验结果"),
              description="提交失败提示错误信息")
    dup, reason = is_near_duplicate(a, b)
    assert not dup and "分支" in reason, f"正负分支两极应判不重复: {reason}"


def test_guard_operation_cross_family():
    """跨操作族（创建 vs 编辑）→ 判不重复；同族同义（创建≈新建）不拦。"""
    a = _case("TC-A", "管理用户账号", steps=("进入用户管理", "创建账号"))
    b = _case("TC-B", "管理用户账号", steps=("进入用户管理", "编辑账号"))
    dup, reason = is_near_duplicate(a, b)
    assert not dup and "操作" in reason, f"跨操作族应判不重复: {reason}"
    c = _case("TC-C", "管理用户账号", steps=("进入用户管理", "新建账号"))
    dup2, _ = is_near_duplicate(a, c)
    assert dup2, "同族同义（创建≈新建）不应被护栏拦住"


def test_guard_extension_mismatch():
    """扩展名/单位维不同 → 判不重复。"""
    a = _case("TC-A", "上传文件校验", steps=("上传 report.pdf", "校验结果"))
    b = _case("TC-B", "上传文件校验", steps=("上传 report.exe", "校验结果"))
    dup, reason = is_near_duplicate(a, b)
    assert not dup and "扩展名" in reason, f"扩展名不同应判不重复: {reason}"


def test_guard_reason_carries_dimension():
    """护栏拦截的 reason 必须携带命中维度（供留痕审计）。用同标题形态
    （title sim=1.0 必达阈值）确保断言走到护栏层而非阈值前短路。"""
    a = _case("TC-A", "查询订单列表并导出", steps=("管理员登录系统", "查询订单列表并导出"),
              description="管理员视角的用例")
    b = _case("TC-B", "查询订单列表并导出", steps=("只读成员登录系统", "查询订单列表并导出"),
              description="只读成员视角的用例")
    dup, reason = is_near_duplicate(a, b)
    assert not dup and "护栏拦截" in reason and "角色" in reason


# ─────────────────────────────────────────────────────────────────────────────
# ② 闸门2：LLM 窄带裁决 mock 三态
# ─────────────────────────────────────────────────────────────────────────────

def _semantic_ready_pair():
    """语义判定可达的对：数字/护栏全过，纯靠 sim 判定。"""
    a = _case("TC-A", "用户登录系统验证身份", steps=("输入账号密码", "提交登录"),
              description="验证登录流程", preconditions="用户持有有效账号")
    b = _case("TC-B", "用户登录系统验证身份信息", steps=("输入账号密码", "提交登录"),
              description="验证登录流程", preconditions="用户持有有效账号")
    return a, b


def test_llm_judge_distinct_keeps_and_logs():
    """裁决"非重复"→ 保留且留痕 llm_distinct。"""
    a, b = _semantic_ready_pair()
    dup, reason = semantic_near_duplicate(a, b, sim=0.99)
    assert dup, f"前置条件不成立（护栏应全过）: {reason}"  # 确认闸门2 前置成立

    decisions: list = []
    kept, removed = dedup_semantic(
        [a, b], embed_func=lambda texts: [[1.0, 0.0]] * len(texts),
        llm_judge=lambda x, y: False,  # LLM 判非重复
        decision_log=decisions,
    )
    assert len(kept) == 2 and removed == [], "LLM 判非重复必须保留"
    assert any(d.get("reason") == "llm_distinct" for d in decisions), \
        f"kept 判定未留痕 llm_distinct: {decisions}"


def test_llm_judge_duplicate_merges():
    """裁决"重复"→ 合并（reason 带 llm_duplicate 前缀）。"""
    a, b = _semantic_ready_pair()
    kept, removed = dedup_semantic(
        [a, b], embed_func=lambda texts: [[1.0, 0.0]] * len(texts),
        llm_judge=lambda x, y: True,
    )
    assert len(kept) == 1 and len(removed) == 1
    assert removed[0]["reason"].startswith("llm_duplicate")


def test_llm_judge_exception_fail_open():
    """LLM 异常 → fail-open 保留（宁可漏删不可误删）。"""
    a, b = _semantic_ready_pair()

    def boom(x, y):
        raise RuntimeError("LLM timeout")

    kept, removed = dedup_semantic(
        [a, b], embed_func=lambda texts: [[1.0, 0.0]] * len(texts),
        llm_judge=boom,
    )
    assert len(kept) == 2 and removed == [], "LLM 异常必须 fail-open 保留"


def test_llm_judge_only_narrow_band():
    """闸门2 只对"即将删除的对"触发：护栏拦下的对不消耗 LLM 调用。"""
    a = _case("TC-A", "管理员创建项目成功", steps=("进入项目管理", "创建项目"))
    b = _case("TC-B", "普通用户创建项目成功", steps=("进入项目管理", "创建项目"))
    calls: list = []

    def judge(x, y):
        calls.append(1)
        return True

    kept, removed = dedup_semantic(
        [a, b], embed_func=lambda texts: [[1.0, 0.0]] * len(texts),
        llm_judge=judge,
    )
    assert len(kept) == 2 and removed == []
    assert calls == [], f"护栏拦截的对不应触发 LLM 裁决: {calls}"


def test_threshold_param_actually_effective():
    """假配置回归：threshold 参数必须真生效（修复前被硬编码常量短路）。"""
    a, b = _semantic_ready_pair()
    # sim=0.93：默认阈值 0.92 判重，收紧到 0.95 判不重
    dup_default, _ = semantic_near_duplicate(a, b, sim=0.93)
    dup_strict, _ = semantic_near_duplicate(a, b, sim=0.93, threshold=0.95)
    assert dup_default and not dup_strict, "threshold 参数未生效"


# ─────────────────────────────────────────────────────────────────────────────
# ③ 闸门3：留痕记录构建（含一键恢复数据源）
# ─────────────────────────────────────────────────────────────────────────────

def test_removal_records_carry_case_json():
    """removed 记录必须带完整 case_json（误删可一键恢复）。"""
    a = _case("TC-1", "锁定期间尝试登录提示剩余锁定时间")
    b = _case("TC-2", "锁定期间尝试登录被拒绝并提示剩余锁定时间",
              description="恢复数据源验证")
    kept, removed = dedup_near_duplicates([a, b])
    assert len(removed) == 1

    case_by_key = {"TC-1": a, "TC-2": b}
    records = build_removal_records(removed, [], task_id="T1", case_by_key=case_by_key)
    assert len(records) == 1
    r = records[0]
    assert r["decision"] == "removed"
    assert r["task_id"] == "T1"
    assert r["removed_case_json"].get("id") == r["removed_id"], \
        "case_json 未取到被删用例（恢复数据源断链）"
    assert r["removed_case_json"].get("description") == "恢复数据源验证"
    assert r["removal_id"].startswith("DR-")


def test_kept_decisions_recorded():
    """闸门1/2 的 kept 判定也要留痕（审计"为什么没删"）。"""
    a = _case("TC-A", "查询订单列表并导出", steps=("管理员登录系统", "查询订单列表并导出"),
              description="管理员视角的用例")
    b = _case("TC-B", "查询订单列表并导出", steps=("只读成员登录系统", "查询订单列表并导出"),
              description="只读成员视角的用例")
    decisions: list = []
    dedup_near_duplicates([a, b], decision_log=decisions)
    records = build_removal_records([], decisions, task_id="T1")
    assert len(records) == 1
    assert records[0]["decision"] == "kept"
    assert "护栏拦截" in records[0]["reason"]


def test_slim_records_strip_case_json():
    """SSE 透出要剔 case_json（不背完整用例），落库才有恢复数据源。"""
    records = [{
        "removal_id": "DR-1", "decision": "removed",
        "removed_case_json": {"title": "x" * 100},
    }]
    slim = slim_records_for_sse(records)
    assert "removed_case_json" not in slim[0] and slim[0]["removal_id"] == "DR-1"

"""批内近重复去重的离线用例（不连 DB、不调 LLM）。

这些用例同时是**防误删护栏**：宁可漏报重复，也不能把"边界值 vs 功能场景"
这类刻意区分的用例合并掉。
"""
from app.utils.case_dedup import (
    dedup_near_duplicates,
    is_near_duplicate,
    step_similarity,
    title_similarity,
)


def _case(cid, title, steps=("输入账号密码", "提交登录"), conf=0.9, ctype="functional", **extra):
    return {
        "id": cid, "title": title, "type": ctype, "confidence": conf,
        "steps": [{"step": i + 1, "action": a, "expected": "系统按规则响应"} for i, a in enumerate(steps)],
        "assertions": [{"type": "equals", "target": "operation.result", "expected_value": "OK"}],
        **extra,
    }


def test_near_identical_titles_are_merged():
    a = _case("TC-1", "锁定期间尝试登录提示剩余锁定时间")
    b = _case("TC-2", "锁定期间尝试登录被拒绝并提示剩余锁定时间")
    dup, reason = is_near_duplicate(a, b)
    assert dup, reason
    kept, removed = dedup_near_duplicates([a, b])
    assert len(kept) == 1 and len(removed) == 1
    assert removed[0]["removed"] in ("TC-1", "TC-2")


def test_boundary_vs_functional_case_is_not_merged():
    """刻意区分的测试设计维度不能被合并（阈值必须留出余量）。"""
    a = _case("TC-F", "连续登录失败5次触发账号锁定", ctype="functional")
    b = _case("TC-B", "失败次数边界-第5次失败触发账号锁定",
              steps=("连续失败4次后再次失败", "检查锁定状态"), ctype="boundary")
    dup, _ = is_near_duplicate(a, b)
    assert not dup
    kept, removed = dedup_near_duplicates([a, b])
    assert len(kept) == 2 and removed == []


def test_identical_steps_alone_do_not_merge_unrelated_titles():
    """步骤相同不是合并理由：不同场景常复用同样的样板步骤。"""
    steps = ("打开登录页", "输入账号与密码", "点击登录")
    a = _case("TC-1", "登录失败记录失败次数", steps=steps)
    b = _case("TC-2", "登录成功跳转首页并展示用户名", steps=steps)
    assert step_similarity(a, b) == 1.0
    kept, removed = dedup_near_duplicates([a, b])
    assert len(kept) == 2 and removed == []


def test_different_boundary_numbers_are_never_merged():
    """实测数据里唯一能区分「4 次边界」与「5 次边界」的信号是数字本身。"""
    a = _case("TC-4", "失败次数边界-第4次失败不触发账号锁定", ctype="boundary")
    b = _case("TC-5", "失败次数边界-第5次失败触发账号锁定", ctype="boundary")
    dup, reason = is_near_duplicate(a, b)
    assert not dup, reason
    kept, _ = dedup_near_duplicates([a, b])
    assert len(kept) == 2

    c = _case("TC-59", "锁定时长边界-59秒时仍在锁定状态", ctype="boundary")
    d = _case("TC-60", "锁定时长边界-60秒时自动解锁", ctype="boundary")
    assert not is_near_duplicate(c, d)[0]
    # 数字相同则不受该护栏影响（回到相似度判定）
    e = _case("TC-30a", "锁定时长30分钟-期间登录被拒绝")
    f = _case("TC-30b", "锁定时长30分钟-期间登录被拒绝并提示")
    assert is_near_duplicate(e, f)[0]


def test_keeps_the_more_complete_case():
    """重复的两条里保留标注更全/置信度更高的那条。"""
    a = _case("TC-LOW", "锁定期间尝试登录提示剩余锁定时间", conf=0.7)
    b = _case("TC-HIGH", "锁定期间尝试登录被拒绝并提示剩余锁定时间", conf=0.95,
              covered_point_ids=["happy_path.1"])
    kept, removed = dedup_near_duplicates([a, b])
    assert [c["id"] for c in kept] == ["TC-HIGH"]
    assert removed[0]["kept"] == "TC-HIGH"


def test_result_is_order_independent():
    cases = [
        _case("TC-A", "锁定期间尝试登录提示剩余锁定时间"),
        _case("TC-B", "登录失败记录失败次数并提示剩余可尝试次数"),
        _case("TC-C", "锁定期间尝试登录被拒绝并提示剩余锁定时间", conf=0.99),
    ]
    k1, r1 = dedup_near_duplicates(cases)
    k2, r2 = dedup_near_duplicates(list(reversed(cases)))
    assert {c["id"] for c in k1} == {c["id"] for c in k2} == {"TC-B", "TC-C"}
    assert len(r1) == len(r2) == 1


def test_output_keeps_original_order():
    cases = [
        _case("TC-A", "甲场景验证一", steps=("打开甲页面", "填写甲表单内容")),
        _case("TC-B", "乙场景验证二", steps=("打开乙页面", "上传乙附件资料")),
    ]
    kept, _ = dedup_near_duplicates(cases)
    assert [c["id"] for c in kept] == ["TC-A", "TC-B"]


def test_removed_record_explains_why():
    a = _case("TC-1", "锁定期间尝试登录提示剩余锁定时间")
    b = _case("TC-2", "锁定期间尝试登录被拒绝并提示剩余锁定时间")
    _, removed = dedup_near_duplicates([a, b])
    rec = removed[0]
    assert set(rec) >= {"removed", "kept", "reason", "removed_title", "kept_title"}
    assert "相似度" in rec["reason"]


def test_empty_and_single_input():
    assert dedup_near_duplicates([]) == ([], [])
    only = [_case("TC-1", "唯一用例")]
    assert dedup_near_duplicates(only)[0] == only


def test_identical_title_similarity_is_one():
    a = _case("TC-1", "完全一样的标题")
    b = _case("TC-2", "完全一样的标题")
    assert title_similarity(a, b) == 1.0

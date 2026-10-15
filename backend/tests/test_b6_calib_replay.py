"""docs/29 32 条生产留痕回放（docs/32 B-6 验收第 2 档）。

数据源：`_calib_pairs.json`（32 条真实去重留痕，sim 0.9217~0.9827，
全部由旧 0.92 阈值放行并删除）+ docs/29 §6.2 人工判读标签。

回放结论（2026-10-15 定标实测；2026-10-14 A 方案护栏微调后更新）：
- 误删 10 条全部救回：8 条闸门1 护栏直接拦截（维度见 expected），
  2 条护栏盲区（#15 无整理版对 / #20 英文 product-project 对）进
  闸门2 窄带，mock LLM 裁"非重复"后保留留痕 llm_distinct——
  合起来 10/10 ≥ 验收要求的 9 条；
- 真重复 20 条：16 条仍合并（含 #28——A 方案实体"无主张侧放行"微调后
  从误拦转正合并），4 条（#3/#18/#24/#25）被实体词护栏**误拦保留**——
  词面差异（文件 vs 文档/需求；一例多提"文件"）触发实体维，
  属 docs/32 设计取舍"宁可误拦（多保留）不漏拦（误删）"的安全方向；
  此断言**锁定现状**：若将来有意放宽实体护栏（含两侧非空形态），
  需同步更新本组断言并复核误删面不回退；
- 存疑 2 条（#10/#16）合并（docs/29 判读"存疑"，两可）。

确定性判定（护栏/阈值）用硬断言；LLM 依赖项用 mock（三态已在
test_b6_dedup_guard 锁死，本文件只验链路到达）。
"""
from __future__ import annotations

from app.core.dedup_judge import make_sync_judge  # noqa: F401  （导入完整性）
from app.utils.case_dedup import (
    dedup_semantic,
    semantic_near_duplicate,
)

# (idx, sim, removed_title, kept_title, expected)
# expected: "keep_guard"=护栏拦 / "keep_llm"=护栏过→闸门2救回 /
#           "merge"=仍合并 / "overblock"=真重复被误拦（安全方向，锁现状）
_CALIB = [
    (0, 0.9455, "管理员编辑项目信息成功", "管理员创建项目成功（核心流程）", "keep_guard"),
    (1, 0.9223, "质量门禁未通过，首次回环后仍不通过则终止", "系统校验质量门禁，通过则完成生成流程", "keep_guard"),
    (2, 0.9418, "管理员创建项目成功", "管理员创建项目成功（核心流程）", "merge"),
    (3, 0.9253, "正确填写上传参数并提交.md文件成功创建异步任务", "用户上传合法.md需求文档成功", "overblock"),
    (4, 0.9772, "连续5次登录失败锁定账号30分钟", "连续5次登录失败后账号锁定30分钟", "merge"),
    (5, 0.9623, "任务完成后更新状态，结果可查看", "异步任务完成后状态更新且结果可查看", "merge"),
    (6, 0.9274, "创建归属产品的模块且名称唯一成功", "管理员创建合法名称的产品成功", "keep_guard"),
    (7, 0.9475, "为角色分配空菜单权限数组", "管理员为只读角色分配菜单权限成功", "keep_guard"),
    (8, 0.9273, "管理员创建产品成功", "创建归属产品的项目且版本号唯一成功", "keep_guard"),
    (9, 0.9631, "创建项目并归属产品成功", "创建归属产品的项目且版本号唯一成功", "merge"),
    (10, 0.9606, "纯图片原型无任何语义节点，系统标记纯视觉稿", "原型无语义节点，系统标记proto_no_sem", "merge"),
    (11, 0.9327, "系统正确执行跨文档矛盾检测并生成澄清问题", "用户同批上传需求文档与原型HTML成功并完成来源", "keep_guard"),
    (12, 0.9502, "超过10MB的HTML文件上传失败", "系统拒绝超过10MB的HTML文件", "merge"),
    (13, 0.9522, "用户选择合法.html文件上传成功", "选择合法.html文件上传成功", "merge"),
    (14, 0.9389, "聚合时同时满足标量取最新和列表去重规则的处理", "按规则聚合跨版本知识时标量取最新值列表去重", "merge"),
    (15, 0.9559, "无整理版时检索返回上线时间靠后的多版本条目", "整理版聚合版本数为边界值时的兜底规则", "keep_llm"),
    (16, 0.9453, "列表字段去重合并同标识条目", "按规则聚合跨版本知识时标量取最新值列表去重", "merge"),
    (17, 0.9594, "生成整理版并标记版本来源链", "生成整理版时保留版本来源链且每条知识可追溯", "merge"),
    (18, 0.9746, "上传合法.md格式需求文档成功创建异步任务", "用户上传合法.md文件并关联产品项目成功", "overblock"),
    (19, 0.9827, "完整需求解析流程成功生成结构化SRR", "执行需求解析全流程生成结构化SRR", "merge"),
    # 注意：以下标题与生产留痕一致地保留 25 字符截断形态（_calib_pairs.json
    # 原样），不得补全——补全会改变护栏判定输入，破坏与生产留痕的一致性。
    (20, 0.9298, "Admin creates product wi", "Create project under exi", "keep_llm"),
    (21, 0.9534, "Attempt to export all te", "Export test cases with s", "merge"),
    (22, 0.9545, "上传合法HTML文件完成主流程", "完整上传符合要求的HTML文件成功", "merge"),
    (23, 0.9359, "执行HTML安全剥离全流程生成安全HTML", "正常HTML内容安全剥离流程成功", "merge"),
    (24, 0.9520, "上传合法.md文件并指定产品项目成功创建任务", "上传合法需求文档成功创建异步解析任务", "overblock"),
    (25, 0.9497, "上传合法.md格式文件成功创建异步任务", "上传合法需求文档成功创建异步解析任务", "overblock"),
    (26, 0.9217, "管理员创建产品成功", "创建项目归属产品成功，输入合法名称和版本号", "keep_guard"),
    (27, 0.9681, "创建项目归属产品成功", "创建项目归属产品成功，输入合法名称和版本号", "merge"),
    (28, 0.9716, "上传合法HTML内容，执行安全剥离生成安全HTM", "上传含合法HTML内容的文件，安全剥离后输出安全", "merge"),
    (29, 0.9484, "上传后缀为.docx的合法HTML文件成功", "上传后缀为.markdown的合法HTML文件成", "keep_guard"),
    (30, 0.9260, "存入原型解析结果到原始文本字段", "原型解析结果完整落库流程验证", "merge"),
    (31, 0.9721, "大小刚好10MB的静态原型文件上传成功", "上传大小刚好10MB的文件成功", "merge"),
]


def _pair(rec):
    _, sim, rt, kt, _exp = rec
    return {"id": "R", "title": rt}, {"id": "K", "title": kt}, sim


def test_misdelete_pairs_all_saved():
    """误删 10 条全救回（验收 2：≥9 应保留全救回）。

    keep_guard：闸门1 护栏拦截，reason 必须带命中维度；
    keep_llm：护栏全过（进闸门2 窄带）→ mock LLM 裁"非重复"→ 保留留痕。
    """
    guard_saved, llm_saved = [], []
    for rec in _CALIB:
        if rec[4] not in ("keep_guard", "keep_llm"):
            continue
        a, b, sim = _pair(rec)
        dup, reason = semantic_near_duplicate(a, b, sim=sim)
        if rec[4] == "keep_guard":
            assert not dup, f"#{rec[0]} 误删对未被护栏拦截: {reason}"
            assert "护栏拦截" in reason, f"#{rec[0]} reason 缺命中维度: {reason}"
            guard_saved.append(rec[0])
        else:
            # 护栏盲区 → 必须进闸门2 窄带（判定为"疑似重复"才会问 LLM）
            assert dup, f"#{rec[0]} 未进闸门2 窄带（无法被 LLM 救回）: {reason}"
            llm_saved.append(rec[0])
    assert len(guard_saved) == 8, f"护栏救回数变化: {guard_saved}"
    assert len(llm_saved) == 2, f"窄带救回数变化: {llm_saved}"


def test_llm_blind_spot_pairs_saved_via_gate2():
    """护栏盲区 2 对经闸门2 mock 裁决后保留，且留痕 llm_distinct。"""
    for rec in _CALIB:
        if rec[4] != "keep_llm":
            continue
        a, b, sim = _pair(rec)
        decisions: list = []
        kept, removed = dedup_semantic(
            [a, b], embed_func=lambda texts: [[sim, 0.0]] * len(texts),
            llm_judge=lambda x, y: False,  # LLM 裁"非重复"
            decision_log=decisions,
        )
        assert len(kept) == 2 and removed == [], f"#{rec[0]} 盲区对未被 LLM 救回"
        assert any(d.get("reason") == "llm_distinct" for d in decisions), \
            f"#{rec[0]} 未留痕 llm_distinct"


def test_true_duplicates_still_merged():
    """真重复仍合并主体（16/20 + 存疑 2 条）——回归锁死当前合并面。

    2026-10-14 A 方案实体"无主张侧放行"微调后 #28 从误拦转正合并（15→16）。
    """
    merged = []
    for rec in _CALIB:
        if rec[4] != "merge":
            continue
        a, b, sim = _pair(rec)
        dup, reason = semantic_near_duplicate(a, b, sim=sim)
        assert dup, f"#{rec[0]} 真重复未能合并（过度拦截）: {reason}"
        merged.append(rec[0])
    assert len(merged) == 18, f"合并面变化（含存疑 2 条 + #28 转正）: {merged}"


def test_overblocks_locked_as_safe_direction():
    """真重复被实体词护栏误拦的 4 对——锁定现状（安全方向：多保留）。

    docs/32 设计取舍「宁可误拦（多保留）不漏拦（误删合并）」：这 4 对是
    词面差异（文件 vs 文档/需求、一例多提"文件"）触发实体维的已知代价
    （两侧非空形态）。若将来放宽两侧非空的实体护栏，必须同步改本断言
    并复核误删面不回退。（#28 已于 2026-10-14 因"一侧为空放行"微调转正。）
    """
    overblocked = []
    for rec in _CALIB:
        if rec[4] != "overblock":
            continue
        a, b, sim = _pair(rec)
        dup, reason = semantic_near_duplicate(a, b, sim=sim)
        assert not dup, f"#{rec[0]} 预期被误拦（锁现状）但判了合并"
        assert "护栏拦截" in reason, f"#{rec[0]} reason 异常: {reason}"
        overblocked.append(rec[0])
    assert len(overblocked) == 4, f"误拦面变化: {overblocked}"


def test_replay_distribution_summary():
    """分布汇总（一次性验收核对，数字变化必须人工复核）。

    2026-10-14 A 方案后分布：keep_guard 8 / keep_llm 2 / merge 18 / overblock 4
    （#28 overblock→merge；误删救回面 10 条零变化）。
    """
    counts: dict = {}
    for rec in _CALIB:
        counts[rec[4]] = counts.get(rec[4], 0) + 1
    assert counts == {
        "keep_guard": 8, "keep_llm": 2, "merge": 18, "overblock": 4,
    }, f"32 对判定分布变化: {counts}"

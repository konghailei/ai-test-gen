"""B-6 A 方案：闸门2 窄带下探 0.88–0.92（不对称默认值）+ 护栏"无主张侧放行"微调。

背景（2026-10-14 大哥拍板）：docs/31 320 条生产口径二跑回放显示 4 处跨分片
真重复 6 对的 sim 落在 0.8988~0.9185，全部低于 0.92 阈值删不掉；而须保留
边界对 0.8935 起与之犬牙交错——阈值手段结构性失效。处置 = 窄带送 LLM 裁决：

- sim ≥ 0.92（≥0.92 带）：维持现状判定链（LLM 否决才留）；
- sim ∈ [0.88, 0.92)（A 窄带）且护栏全过：**默认保留，LLM 明确确认才删**；
- sim < 0.88：不比较；LLM 缺席/失败一律 fail-open 保留。

护栏微调（同批）："无主张侧放行"——
- 实体：一侧为空（∅）放行，两侧非空不等仍拦（含子集形态，docs/29 #11
  误删对 {文档} ⊂ {需求,文档} 实证回归锁）；
- 扩展名/单位：与操作词同构「互不包含才拦」（真重复对常单侧多提单位）。

全部确定性断言（不调 LLM、不连 DB）。
"""
from __future__ import annotations

from app.utils.case_dedup import (
    SEMANTIC_BAND_FLOOR,
    _structural_guards,
    dedup_semantic,
)

# ─────────────────────────────────────────────────────────────────────────────
# 工具：可控余弦的 embed（两例 sim 精确给定）
# ─────────────────────────────────────────────────────────────────────────────

def _embed_with(sim: float):
    """返回 embed_func：两条用例的余弦 = sim（后续条目复用第二向量）。"""
    from math import sqrt

    v0 = [1.0, 0.0]
    v1 = [sim, sqrt(max(0.0, 1.0 - sim * sim))]

    def _embed(texts):
        return [list(v0) if i == 0 else list(v1) for i in range(len(texts))]

    return _embed


def _pair(title_a: str, title_b: str, steps_a=(), steps_b=()):
    a = {"id": "TC-A", "title": title_a,
         "steps": [{"action": s} for s in steps_a]}
    b = {"id": "TC-B", "title": title_b,
         "steps": [{"action": s} for s in steps_b]}
    return a, b


# 护栏全过的窄带候选对（无数字/角色/实体/单位差异）
_BAND_PAIR = ("系统验证重复数据检测逻辑", "系统验证重复数据检测逻辑处理")


# ─────────────────────────────────────────────────────────────────────────────
# ① A 窄带行为（不对称默认值）
# ─────────────────────────────────────────────────────────────────────────────

def test_band_llm_confirmed_merges():
    """A 窄带 + LLM 确认"同一场景" → 合并，reason 带 llm_band_duplicate。"""
    a, b = _pair(*_BAND_PAIR)
    kept, removed = dedup_semantic(
        [a, b], embed_func=_embed_with(0.90), llm_judge=lambda x, y: True,
    )
    assert len(kept) == 1 and len(removed) == 1
    assert removed[0]["reason"].startswith("llm_band_duplicate"), removed


def test_band_llm_distinct_keeps_and_logs():
    """A 窄带 + LLM 判非重复 → 保留并留痕 llm_band_distinct。"""
    a, b = _pair(*_BAND_PAIR)
    decisions: list = []
    kept, removed = dedup_semantic(
        [a, b], embed_func=_embed_with(0.90), llm_judge=lambda x, y: False,
        decision_log=decisions,
    )
    assert len(kept) == 2 and removed == []
    assert any(d.get("reason") == "llm_band_distinct" for d in decisions), decisions


def test_band_without_judge_keeps_by_default():
    """不对称默认值：LLM 缺席时 A 窄带一律保留（0.88–0.92 不再直接删除）。"""
    a, b = _pair(*_BAND_PAIR)
    kept, removed = dedup_semantic([a, b], embed_func=_embed_with(0.90))
    assert len(kept) == 2 and removed == [], "无 LLM 时窄带必须默认保留"


def test_band_judge_exception_fail_open():
    """窄带裁决异常 → fail-open 保留。"""
    a, b = _pair(*_BAND_PAIR)

    def boom(x, y):
        raise RuntimeError("LLM timeout")

    kept, removed = dedup_semantic([a, b], embed_func=_embed_with(0.90), llm_judge=boom)
    assert len(kept) == 2 and removed == []


def test_band_guard_blocked_pair_not_judged():
    """护栏拦下的窄带候选不消耗 LLM 调用（先确定性后 LLM）。"""
    a, b = _pair("管理员创建项目成功", "普通用户创建项目成功")  # 角色词不同
    calls: list = []

    def judge(x, y):
        calls.append(1)
        return True

    kept, removed = dedup_semantic([a, b], embed_func=_embed_with(0.90), llm_judge=judge)
    assert len(kept) == 2 and removed == [], "护栏拦截的对不得合并"
    assert calls == [], f"护栏拦截的对不应触发 LLM: {calls}"


def test_below_band_floor_not_judged():
    """sim < 0.88 不进窄带、不消耗 LLM、不合并。"""
    a, b = _pair(*_BAND_PAIR)
    calls: list = []

    def judge(x, y):
        calls.append(1)
        return True

    kept, removed = dedup_semantic([a, b], embed_func=_embed_with(0.87), llm_judge=judge)
    assert len(kept) == 2 and removed == []
    assert calls == []


def test_band_floor_constant():
    """窄带下限定标值（改动需重新走 docs/31 回放定标）。"""
    assert SEMANTIC_BAND_FLOOR == 0.88


# ─────────────────────────────────────────────────────────────────────────────
# ② 护栏"无主张侧放行"微调
# ─────────────────────────────────────────────────────────────────────────────

def test_unit_one_side_empty_passes():
    """单位一侧为空 = 无主张，放行（docs/31 幂等对 [] vs ['次'] 形态）。

    真实对：'重复提交同一向量时幂等处理不重复入库'（步骤含 2 次）vs
    '整理版向量入库的幂等性边界测试（重复投递）'。
    """
    a, b = _pair(
        "重复提交同一向量时幂等处理不重复入库",
        "整理版向量入库的幂等性边界测试",
        steps_a=("触发整理版聚合完成事件", "系统生成向量并重复提交入库请求2次"),
    )
    ok, reason = _structural_guards(a, b)
    assert ok, f"单位一侧为空不应拦截: {reason}"


def test_unit_conflict_still_blocked():
    """单位真冲突（MB vs GB）仍拦。"""
    a, b = _pair("上传大小10MB的文件成功", "上传大小10GB的文件成功")
    ok, reason = _structural_guards(a, b)
    assert not ok and "单位不同" in reason, reason


def test_extension_conflict_still_blocked():
    """扩展名真冲突（.docx vs .markdown）仍拦（校准 #29 keep_guard 回归锁）。"""
    a, b = _pair("上传后缀为.docx的合法文件成功", "上传后缀为.markdown的合法文件成功")
    ok, reason = _structural_guards(a, b)
    assert not ok and "扩展名不同" in reason, reason


def test_extension_one_side_empty_passes():
    """扩展名一侧为空 = 详略差异，放行。"""
    a, b = _pair("上传后缀为.docx的合法文件成功", "上传合法文件成功")
    ok, reason = _structural_guards(a, b)
    assert ok, f"扩展名一侧为空不应拦截: {reason}"


def test_entity_one_side_empty_passes():
    """实体一侧为空放行（docs/31 边界对「…400的HTML文件」vs「语义树节点数边界」形态）。"""
    a, b = _pair("语义树节点数边界（400个节点）", "解析生成语义树节点数刚好400的HTML文件")
    ok, reason = _structural_guards(a, b)
    assert ok, f"实体一侧为空不应拦截: {reason}"


def test_entity_subset_still_blocked_regression_11():
    """docs/29 #11 生产误删对回归锁：实体子集形态仍拦，不得放宽。

    '跨文档矛盾检测'（{文档}）vs '上传需求文档'（{需求,文档}）——
    A ⊂ B 的子集形态就是当年误删的形态，两侧非空不等一律拦。
    """
    a, b = _pair(
        "系统正确执行跨文档矛盾检测并生成澄清问题",
        "用户同批上传需求文档与原型HTML成功并完成来源记录",
    )
    ok, reason = _structural_guards(a, b)
    assert not ok and "实体词不同" in reason, f"#11 回归（实体子集必须拦）: {reason}"

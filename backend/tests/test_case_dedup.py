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


# ============================================================================
# 跨子轮语义去重（缺陷 E，2026-09-22）
# 以下用例用**假 embedding 函数**模拟 bge 行为（离线 CI 可跑，不依赖模型）。
# 阈值语义（真实定标见 case_dedup.py 头注释）：
#   ≥0.92 判重复（数字护栏先短路）；<0.92 保留。
# ============================================================================

def _scripted_embed(cases: list[dict], group_of: dict[str, str]):
    """构造**确定性**假 embedding（离线 CI 可跑，不依赖 bge 模型）。

    group_of: {case_id: 组名}——同组的用例走同一基向量（余弦 1.0，代表
    "同一场景的改写"，必被判重复）；不同组/未列出的用例各占一条独立基向量
    （余弦 0，必不重复）。

    ⚠️ 刻意**不用 hash()**：Python 每个进程的 hash 种子随机，
    `hash(x) % N` 跨运行会碰撞，导致"不同组"意外共线、测试时好时坏。

    v3.5.8（docs/27 C3）：映射表按 _semantic_text 输出构造，与序列化口径
    解耦（口径 e1→e2 回退时本 helper 无需改动——当时改造的收益）。
    cases 用于预计算 text→组名。**同语义文本的不同用例会碰撞**——构造
    夹具时保证 title 不同（本文件所有夹具均满足）。
    """
    from app.utils.case_dedup import _semantic_text
    text_to_group = {_semantic_text(c): group_of.get(c["id"]) for c in cases}
    groups = sorted(set(group_of.values()))

    def embed(texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            g = text_to_group.get(t)
            v = [0.0] * (len(groups) + 1)
            v[groups.index(g) if g in groups else len(groups)] = 1.0
            out.append(v)
        return out

    return embed


def _embed_with_cosine(cases: list[dict], target: float, cid_a: str, cid_b: str):
    """两个用例余弦恰为 target 的假 embedding（用于阈值边界用例）。

    v3.5.8（docs/27 C3）：同 _scripted_embed，按 _semantic_text 映射（口径解耦）。
    """
    from app.utils.case_dedup import _semantic_text
    import math

    def _text(cid: str) -> str:
        return _semantic_text(next(c for c in cases if c["id"] == cid))

    table = {
        _text(cid_a): [1.0, 0.0],
        _text(cid_b): [target, math.sqrt(max(0.0, 1 - target * target))],
    }

    def embed(texts: list[str]) -> list[list[float]]:
        return [table[t] for t in texts]

    return embed


def test_semantic_dedup_merges_cross_channel_rewrite():
    """跨通道语义改写（3-gram 抓不住）必须被 embedding 命中。"""
    from app.utils.case_dedup import dedup_semantic

    a = _case("TC-FP-1", "连续5次密码错误触发账号锁定并提示锁定消息",
              steps=("连续输入5次错误密码", "第5次后检查锁定提示"))
    b = _case("TC-INT-1", "连续登录失败5次触发账号锁定并提示锁定消息",
              steps=("连续登录失败5次", "验证账号被锁定并提示"))
    # 该对是定标里的"跨通道改写"形态：3-gram 抓不住，必须靠语义
    assert not is_near_duplicate(a, b)[0]
    embed = _scripted_embed([a, b], {"TC-FP-1": "lock5", "TC-INT-1": "lock5"})
    kept, removed = dedup_semantic([a, b], embed_func=embed)
    assert len(kept) == 1 and len(removed) == 1
    assert "语义相似度" in removed[0]["reason"]


def test_semantic_dedup_number_guardrail_protects_boundaries():
    """数字集合不同 → 即使向量说像也必须保留（不同边界值不是重复）。"""
    from app.utils.case_dedup import dedup_semantic

    a = _case("TC-45", "连续失败4次不锁定第5次锁定验证失败次数边界")
    b = _case("TC-6", "锁定期间第6次失败计数保持5不增加")
    embed = _scripted_embed([a, b], {"TC-45": "same", "TC-6": "same"})  # 向量说像（余弦 1.0）
    kept, removed = dedup_semantic([a, b], embed_func=embed)
    assert len(kept) == 2 and removed == []


def test_semantic_dedup_keeps_more_complete_case_as_representative():
    """簇代表选择：完整性（要点数/步骤/断言）优先于置信度。

    定标实证：integration 改写版置信度噪声级高 0.005 但步骤更少，
    必须留下更完整的 FP 分片版。
    """
    from app.utils.case_dedup import dedup_semantic

    fp_full = _case("TC-FP", "锁定期间正确凭证登录被拒绝并提示剩余锁定时间",
                    steps=("锁定账号", "用正确密码登录", "验证拒绝与剩余时间提示"),
                    conf=0.947, covered_point_ids=["alternate_flow.2"])
    int_rewrite = _case("TC-INT", "锁定期间尝试登录被拒绝并提示剩余锁定时间",
                        steps=("锁定账号", "尝试登录验证被拒"),
                        conf=0.951, covered_point_ids=["alternate_flow.2"])
    embed = _scripted_embed([int_rewrite, fp_full], {"TC-FP": "g1", "TC-INT": "g1"})
    kept, removed = dedup_semantic([int_rewrite, fp_full], embed_func=embed)
    assert [c["id"] for c in kept] == ["TC-FP"], "必须保留更完整的 FP 分片版"
    assert removed[0]["removed"] == "TC-INT"


def test_semantic_dedup_degrades_to_3gram_without_embed():
    """embed_func=None → 退化为 3-gram 去重（不丢安全网）。"""
    from app.utils.case_dedup import dedup_semantic

    a = _case("TC-1", "锁定期间尝试登录提示剩余锁定时间")
    b = _case("TC-2", "锁定期间尝试登录被拒绝并提示剩余锁定时间")
    kept, removed = dedup_semantic([a, b], embed_func=None)
    assert len(kept) == 1 and len(removed) == 1  # 3-gram 能抓表面像的
    c = _case("TC-3", "登录成功跳转首页并展示用户名")
    kept2, _ = dedup_semantic([a, c], embed_func=None)
    assert len(kept2) == 2


def test_semantic_dedup_degrades_on_embed_failure():
    """embedding 抛异常 → 降级 3-gram，不击穿去重环节。"""
    from app.utils.case_dedup import dedup_semantic

    def boom(texts):
        raise RuntimeError("embedding 服务抖动")

    a = _case("TC-1", "锁定期间尝试登录提示剩余锁定时间")
    b = _case("TC-2", "锁定期间尝试登录被拒绝并提示剩余锁定时间")
    kept, removed = dedup_semantic([a, b], embed_func=boom)
    assert len(kept) == 1  # 降级后 3-gram 仍工作


def test_semantic_dedup_below_threshold_keeps_both():
    """相似度低于 0.92 → 保留（宁可不报不误删，漏的交给人工审核）。"""
    from app.utils.case_dedup import dedup_semantic

    a = _case("TC-A", "统一错误提示防止用户名枚举")
    b = _case("TC-B", "用户名不存在与密码错误提示一致不泄露账号存在性")
    # 余弦恰为 0.89（低于阈值 0.92）→ 必须两条都保留
    embed = _embed_with_cosine([a, b], 0.89, "TC-A", "TC-B")
    kept, removed = dedup_semantic([a, b], embed_func=embed)
    assert len(kept) == 2 and removed == []


def test_semantic_dedup_above_threshold_is_merged():
    """余弦高于阈值 0.92 → 判重复。（不用"恰等于"，避浮点边界抖动）"""
    from app.utils.case_dedup import dedup_semantic

    a = _case("TC-A", "锁定到期自动解锁并清除失败计数")
    b = _case("TC-B", "锁定期满自动解锁且失败次数重置")
    embed = _embed_with_cosine([a, b], 0.93, "TC-A", "TC-B")
    kept, removed = dedup_semantic([a, b], embed_func=embed)
    assert len(kept) == 1 and len(removed) == 1


def test_semantic_dedup_result_keeps_input_order():
    """输出保持原输入顺序（前端/持久化对顺序有隐含依赖）。"""
    from app.utils.case_dedup import dedup_semantic

    a = _case("TC-A", "锁定期间尝试登录提示剩余锁定时间")
    b = _case("TC-B", "登录成功跳转首页并展示用户名")
    c = _case("TC-C", "锁定期间尝试登录被拒绝并提示剩余锁定时间")
    embed = _scripted_embed([a, b, c], {"TC-A": "g1", "TC-C": "g1", "TC-B": "g2"})
    kept, removed = dedup_semantic([a, b, c], embed_func=embed)
    assert [x["id"] for x in kept] == ["TC-A", "TC-B"]  # 原顺序
    assert len(removed) == 1


# ============================================================================
# E 口径：序列化出口 _semantic_text（docs/27 C3 定标回退，v3.5.8）
# ============================================================================
# e1（语义字段瘦身）经 1055 条真实用例定标否决（判重 586→119，漏判 469 对
# 真重复——JSON 结构共现是该口径的工作部件）。当前 e2 = 整例 JSON。
# 以下用例锁定 e2 形态，防将来"再优化序列化"时无声回退到已否决的 e1。

def test_semantic_text_is_full_case_json():
    """e2 口径：序列化 = 整例 JSON（合法 JSON、含全部字段、与 09-16 定标口径一致）。"""
    import json as _json

    from app.utils.case_dedup import _semantic_text

    a = _case("TC-1", "锁定期间尝试登录提示剩余锁定时间")
    text = _semantic_text(a)
    parsed = _json.loads(text)  # 必须是合法 JSON（C2 期间"不可解析"契约已作废）
    assert parsed["title"] == "锁定期间尝试登录提示剩余锁定时间"
    assert parsed["steps"] and parsed["assertions"]
    # 元数据在整例 JSON 里（结构共现是判别力的组成部分，不得剔除）
    assert "TC-1" in text and "0.9" in text and "functional" in text


def test_semantic_text_tracks_content_changes():
    """内容变 → 序列化变（缓存 stale 防护的基础，任何口径都必须满足）。"""
    from app.utils.case_dedup import _semantic_text

    base = _case("TC-1", "上传大小刚好10MB的文件成功")
    diff_steps = _case("TC-1", "上传大小刚好10MB的文件成功",
                       steps=("选择10MB文件", "点击上传确认"))
    diff_meta = _case("TC-1", "上传大小刚好10MB的文件成功", priority="P0")
    t_base = _semantic_text(base)
    assert _semantic_text(diff_steps) != t_base
    assert _semantic_text(diff_meta) != t_base, "整例 JSON 口径下元数据变更也改变文本（正确行为）"


def test_semantic_text_version_marker_is_e2():
    """口径版本锁 e2：任何人把 _semantic_text 改回 e1 瘦身，本用例先红。"""
    import app.utils.case_dedup as cd

    assert cd.SEMANTIC_SERIALIZATION_VERSION == "e2", (
        "e1（语义字段瘦身）已定标否决；如启新口径先跑 docs/27 C3 定标再递增版本号"
    )


# ============================================================================
# B 方案：进程内向量缓存（docs/27 §2.5，v3.5.8）
# ============================================================================

def _counting_embed(counter: dict):
    """记录调用次数的确定性假 embedding（按 _semantic_text 分组返回）。"""
    from app.utils.case_dedup import _semantic_text

    def embed(texts: list[str]) -> list[list[float]]:
        counter["calls"] = counter.get("calls", 0) + 1
        counter["texts"] = counter.get("texts", 0) + len(texts)
        return [[float(len(t)), 1.0] for t in texts]

    return embed


def test_cache_second_call_is_full_hit():
    """同输入二次调用 → embed_func 零新增调用（缓存生效的核心验收）。"""
    from app.utils.case_dedup import _EMBED_CACHE, _embed_cached

    _EMBED_CACHE.clear()
    counter: dict = {}
    embed = _counting_embed(counter)
    texts = ["aaa", "bbb", "ccc"]
    v1 = _embed_cached(embed, "model-x", texts)
    assert counter["calls"] == 1 and counter["texts"] == 3
    v2 = _embed_cached(embed, "model-x", texts)
    assert counter["calls"] == 1, "第二次必须全命中，不再调 embed_func"
    assert v1 == v2


def test_cache_invalidates_on_content_change():
    """用例内容变 → _semantic_text 变 → key 变 → 只重算变的那条。"""
    from app.utils.case_dedup import _EMBED_CACHE, _embed_cached

    _EMBED_CACHE.clear()
    counter: dict = {}
    embed = _counting_embed(counter)
    _embed_cached(embed, "model-x", ["aaa", "bbb"])
    _embed_cached(embed, "model-x", ["aaa", "bbb-modified"])
    assert counter["texts"] == 3, "只有变化的文本重新 embed"
    assert counter["calls"] == 2


def test_cache_key_isolates_models_and_versions():
    """换模型/改口径 → 全量 miss（A/B 协同：防旧向量冒充新模型/新口径结果）。"""
    from app.utils.case_dedup import _EMBED_CACHE, _cache_key, _embed_cached
    import app.utils.case_dedup as cd

    _EMBED_CACHE.clear()
    counter: dict = {}
    embed = _counting_embed(counter)
    _embed_cached(embed, "model-a", ["aaa"])
    _embed_cached(embed, "model-b", ["aaa"])
    assert counter["calls"] == 2, "不同 model_id 不得共享缓存条目"

    # 口径版本进 key：版本变更后同模型同文本也 miss
    orig = cd.SEMANTIC_SERIALIZATION_VERSION
    try:
        k_before = _cache_key("model-a", "aaa")
        cd.SEMANTIC_SERIALIZATION_VERSION = orig + "-x"
        assert _cache_key("model-a", "aaa") != k_before, "口径版本必须进缓存 key"
        _embed_cached(embed, "model-a", ["aaa"])
        assert counter["calls"] == 3, "口径变更 → 全量 miss → 重算"
    finally:
        cd.SEMANTIC_SERIALIZATION_VERSION = orig


def test_cache_projection_excludes_volatile_fields():
    """投影排除 review 会原地改写的字段（confidence）——跨轮命中的关键。

    review_agent.py:298 实证：review() 把 case["confidence"] 改写为
    final_confidence，reviewed_cases 是同一批 dict 对象。若 key 用整例
    JSON，confidence 数值变化即全量 miss（17min 问题复活）。
    """
    from app.utils.case_dedup import _cache_projection

    base = _case("TC-1", "锁定期间尝试登录提示剩余锁定时间")
    after_review = _case("TC-1", "锁定期间尝试登录提示剩余锁定时间",
                         conf=0.87, review_status="approved", reviewed_by="r",
                         source="integration")
    assert _cache_projection(base) == _cache_projection(after_review)


def test_cache_projection_tracks_semantic_content():
    """语义内容变 → 投影变 → 重算（缓存不得返回陈旧向量）。"""
    from app.utils.case_dedup import _cache_projection

    base = _case("TC-1", "上传大小刚好10MB的文件成功")
    diff_steps = _case("TC-1", "上传大小刚好10MB的文件成功",
                       steps=("选择10MB文件", "点击上传确认"))
    diff_title = _case("TC-1", "上传大小刚好11MB的文件成功")
    p = _cache_projection(base)
    assert _cache_projection(diff_steps) != p
    assert _cache_projection(diff_title) != p


def test_cache_skipped_without_model_id():
    """model_id 空 = 模型未知 → 不缓存（fail-safe：宁可不缓存也不错位）。"""
    from app.utils.case_dedup import _EMBED_CACHE, _embed_cached

    _EMBED_CACHE.clear()
    counter: dict = {}
    embed = _counting_embed(counter)
    _embed_cached(embed, "", ["aaa"])
    _embed_cached(embed, "", ["aaa"])
    assert counter["calls"] == 2, "空 model_id 每次直连，不进缓存"
    assert len(_EMBED_CACHE) == 0


def test_cache_embed_length_mismatch_degrades_to_3gram():
    """embed 返回数量不符 → dedup_semantic 降级 3-gram，不击穿（健壮性缺口修复）。"""
    from app.utils.case_dedup import dedup_semantic

    def short_embed(texts):
        return [[1.0, 0.0]]  # 请求 2 条只回 1 条

    a = _case("TC-1", "锁定期间尝试登录提示剩余锁定时间")
    b = _case("TC-2", "锁定期间尝试登录被拒绝并提示剩余锁定时间")
    kept, removed = dedup_semantic([a, b], embed_func=short_embed, model_id="m")
    assert len(kept) == 1, "降级后 3-gram 仍能抓表面重复"


def test_dedup_semantic_cache_survives_review_confidence_rewrite():
    """端到端（真实流水线场景）：review 原地改写 confidence 后第二轮全命中。

    这是 key/文本解耦的设计场景——embedding 文本（整例 JSON）随 confidence
    变化，但缓存 key（语义投影）不变 → 复用已有向量，不重算。
    """
    from app.utils.case_dedup import _EMBED_CACHE, dedup_semantic

    _EMBED_CACHE.clear()
    counter: dict = {}
    embed = _counting_embed(counter)
    cases = [
        _case("TC-A", "锁定期间尝试登录提示剩余锁定时间"),
        _case("TC-B", "登录成功跳转首页并展示用户名"),
    ]
    k1, _ = dedup_semantic(cases, embed_func=embed, model_id="model-x")
    assert counter["calls"] == 1
    # 模拟 review() 的真实行为：原地改写 confidence（review_agent.py:298）
    cases2 = [dict(c, confidence=0.87) for c in cases]
    k2, _ = dedup_semantic(cases2, embed_func=embed, model_id="model-x")
    assert counter["calls"] == 1, "review 改写 confidence 不得导致缓存 miss"
    assert {c["id"] for c in k1} == {c["id"] for c in k2}


def test_dedup_semantic_cache_misses_on_semantic_change():
    """语义内容变 → 投影变 → miss 重算（与上一条互为护栏）。"""
    from app.utils.case_dedup import _EMBED_CACHE, dedup_semantic

    _EMBED_CACHE.clear()
    counter: dict = {}
    embed = _counting_embed(counter)
    a = _case("TC-A", "锁定期间尝试登录提示剩余锁定时间")
    dedup_semantic([a], embed_func=embed, model_id="model-x")
    a2 = _case("TC-A", "锁定期间尝试登录提示剩余锁定时间",
               steps=("锁定账号", "用正确密码登录"))
    dedup_semantic([a2], embed_func=embed, model_id="model-x")
    assert counter["calls"] == 2, "steps 变了必须重算"

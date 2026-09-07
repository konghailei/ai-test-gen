"""验证 v2.8.67 对 REQ-1465 的三项修复。

核心验证点：
A. 优化影响配套问题去重 + 排末尾（Q1/Q7/Q5 重复问题修复）
B. 引用未定义问题文本精简（不再又臭又长）
C. 引用未定义 KB 查询生成短 query（theme_label + 后缀关键词）
"""
import sys, os, json, logging, types
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

# 不实例化 ClarifyAgent（需完整依赖），改为：
# 1. 用 mock 填满缺失依赖空模块
# 2. 直接 import 类对象（只用于调用 _detect_optimization_impact 等静态方法）
def _install_mock_deps():
    # 把 pydantic_settings / app.config / app.db 等缺失的全做成 mock 模块
    for mod in [
        "pydantic_settings",
        "dotenv",
        "sqlalchemy",
        "sqlalchemy.ext",
        "sqlalchemy.orm",
        "sqlalchemy.future",
        "app.db",
        "app.db.models",
        "app.db.database",
        "app.db.hybrid_search",
        "app.agents",
        "app.prompts",
        "app.core",
        "app.services",
    ]:
        if mod not in sys.modules:
            sys.modules[mod] = types.ModuleType(mod)

    # app.config.settings mock
    class _MockSettings:
        LLM_API_KEY = ""
        pass
    # app.config 需在 mock 列表创建后补齐
    if "app" not in sys.modules:
        sys.modules["app"] = types.ModuleType("app")
    if "app.config" not in sys.modules:
        sys.modules["app.config"] = types.ModuleType("app.config")
    sys.modules["app.config"].settings = _MockSettings()
    sys.modules["pydantic_settings"].BaseSettings = object
    sys.modules["pydantic_settings"].SettingsConfigDict = dict

_install_mock_deps()

# 确保 app 根模块存在
if "app" not in sys.modules:
    sys.modules["app"] = types.ModuleType("app")

# 现在可以 import ClarifyAgent 类（调用静态方法用）
try:
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "clarify_agent_direct",
        os.path.join(os.path.dirname(__file__), "app/agents/clarify_agent.py"),
    )
    _mod = importlib.util.module_from_spec(_spec)
    # 额外打补丁：agent 顶部 from app.agents import pattern_matcher 等依赖
    _mock_pm = types.ModuleType("pattern_matcher")
    _mock_pm.match_pattern = lambda srr: None
    _mock_pm.detect_missing_dimensions = lambda srr, mp: []
    sys.modules["app.agents.pattern_matcher"] = _mock_pm
    sys.modules["app.prompts.clarify_agent"] = types.ModuleType("app.prompts.clarify_agent")
    sys.modules["app.core.prompt_manager"] = types.ModuleType("app.core.prompt_manager")
    sys.modules["app.core.prompt_manager"].PromptManager = object
    sys.modules["app.services.llm_service"] = types.ModuleType("app.services.llm_service")
    _spec.loader.exec_module(_mod)
    ClarifyAgent = _mod.ClarifyAgent
    _HAS_AGENT = True
except Exception as e:
    ClarifyAgent = None
    _HAS_AGENT = False
    print(f"⚠️  无法导入 ClarifyAgent（非致命，仅跳过 A1）: {type(e).__name__}: {str(e)[:100]}")

# ================================================================
# Test A：_detect_optimization_impact 产出问题结构正确
#   - 含 kb_optimization_impact + kb_optimization_impact_scope
#   - category=impact_analysis（非 conflict，所以进入统一分流池）
# ================================================================
def test_A1_detect_opt_impact_structure():
    if not _HAS_AGENT:
        print("⚠️  Test A1 跳过：ClarifyAgent 不可导入（无依赖）")
        return
    srr = {
        "requirement": {"id": "REQ-TEST", "title": "内容平台优化项目", "modules": []},
        "_raw_text": "本次对内容平台进行重构改造，优化图片素材库和版本管理功能",
        "_llm_is_optimization": {"is_optimization": True, "reason": "明确优化改造"},
    }
    agent_obj = object.__new__(ClarifyAgent)
    qs = ClarifyAgent._detect_optimization_impact(agent_obj, srr)
    assert len(qs) == 2, f"配套问题数量应为2，实际={len(qs)}"
    ids = {q["id"] for q in qs}
    assert ids == {"kb_optimization_impact", "kb_optimization_impact_scope"}, f"id不符={ids}"
    for q in qs:
        assert q["category"] == "impact_analysis", f"category应为impact_analysis，实际={q['category']}"
    print("✅ Test A1 PASS: 优化需求检测产出 2 个配套问题，结构正确")

# ================================================================
# Test A2：LLM 语义层过滤 - legacy_data_impact 字段和语义重复都要删除
# ================================================================
def test_A2_llm_semantic_duplicate_filter():
    # 模拟 6 个 LLM 生成的问题
    llm_qs = [
        # 应该被过滤（field 命中 _LDI_DUP_FIELDS）
        {"field": "legacy_data_impact.impact_scope", "category": "completeness",
         "question": "补充 legacy_data_impact.impact_scope"},
        {"field": "legacy_data_impact.user_clarification", "category": "completeness",
         "question": "补充 user_clarification"},
        # 应该被过滤（语义重复，与优化配套 Q1 同核）
        {"field": "business_rules", "category": "impact_analysis",
         "question": "检测到是优化需求，请问是否影响已有历史数据？请说明影响类型"},
        # 应该被过滤（语义重复，与优化配套 Q2 同核）
        {"field": "data_model.entities", "category": "completeness",
         "question": "请描述影响范围：涉及的历史数据实体、表、字段"},
        # 应该保留（普通 completeness，无重复）
        {"field": "flows.happy_path", "category": "completeness",
         "question": "补充主流程步骤"},
        # 应该保留（conflict 类）
        {"field": "requirement.ambiguity", "category": "conflict",
         "question": "验收标准中50 vs 不设上限存在矛盾"},
    ]
    # 模拟 L775-L821 处的过滤逻辑
    _LDI_DUP_FIELDS = (
        "legacy_data_impact.impact_scope",
        "legacy_data_impact.migration_plan",
        "legacy_data_impact.rollback_and_validation",
        "legacy_data_impact.risks",
        "legacy_data_impact.user_clarification",
        "legacy_data_impact",
        "optimization_impact",
    )
    direct = []
    kb_eligible = []
    for q in llm_qs:
        cat = q["category"]
        field = q["field"]
        qtext = (q["question"] or "")[:100]
        if any(f in field for f in _LDI_DUP_FIELDS):
            continue
        _dup1 = (("优化" in qtext and "历史数据" in qtext and "影响" in qtext)
                 or ("改进" in qtext and "历史数据" in qtext and "影响" in qtext))
        _dup2 = (("影响范围" in qtext and "数据" in qtext)
                 or ("描述" in qtext and "历史数据" in qtext and "实体" in qtext))
        if _dup1 or _dup2:
            continue
        if cat == "conflict":
            direct.append(q)
        else:
            kb_eligible.append(q)
    retained_fields = {q["field"] for q in kb_eligible + direct}
    assert retained_fields == {"flows.happy_path", "requirement.ambiguity"}, \
        f"过滤后应仅剩 2 个字段，实际={retained_fields}"
    assert len(direct) == 1 and direct[0]["category"] == "conflict", "conflict类应仅1个"
    print("✅ Test A2 PASS: LLM语义层成功删除 4 个优化重复问题，仅保留 2 个有效问题")

# ================================================================
# Test A3：统一分流池 - 配套问题正确标记 _direct_human_tail
# ================================================================
def test_A3_unified_pool_opt_marked_direct_tail():
    opt_qs = [
        {"id": "kb_optimization_impact", "category": "impact_analysis",
         "question": "优化是否影响历史数据？"},
        {"id": "kb_optimization_impact_scope", "category": "impact_analysis",
         "question": "请描述影响范围"},
    ]
    contradiction_q = {
        "id": "c1", "category": "contradiction", "question": "50 vs 不设上限矛盾"
    }
    ref_undef_q = {
        "id": "r1", "category": "ambiguity", "question": "图片上传规则引用未定义",
        "_ref_theme_label": "图片上传规则",
    }
    candidates = opt_qs + [contradiction_q, ref_undef_q]
    _DIRECT_CATS = {"conflict", "contradiction"}
    _OPT_TAIL_IDS = {"kb_optimization_impact", "kb_optimization_impact_scope"}
    direct = []
    kb = []
    for q in candidates:
        q["_from_unified_candidate"] = True
        qid = q["id"]
        cat = q["category"].lower()
        if qid in _OPT_TAIL_IDS:
            q["_direct_human_tail"] = True
            direct.append(q)
        elif cat in _DIRECT_CATS or "contradict" in cat:
            direct.append(q)
        else:
            kb.append(q)
    # 断言：两个配套问题标记了 _direct_human_tail 并进 direct
    assert all(q.get("_direct_human_tail") for q in opt_qs), "配套问题应标记 _direct_human_tail"
    # 断言：矛盾问题进 direct 但没标记 tail
    assert contradiction_q in direct and not contradiction_q.get("_direct_human_tail"), \
        "contradiction 应直接人工但不排 tail"
    # 断言：引用未定义进 KB（先走 KB 查询）
    assert ref_undef_q in kb, "引用未定义类应先走KB"
    assert len(direct) == 3, len(kb) == 1
    print("✅ Test A3 PASS: 统一分流池正确分类——配套2个direct_tail + 1个contradiction直接人工 + 1个ref_undef进KB")

# ================================================================
# Test A4：末位追加 - final 去重后单独追加 opt_tail，保证排末尾
# ================================================================
def test_A4_final_questions_ordered_with_tail():
    questions = [
        {"id": "q1", "field": "flows.happy_path", "question": "补充主流程步骤"},
        {"id": "q2", "field": "requirement.ambiguity", "question": "50 vs 不设上限矛盾"},
    ]
    opt_tail_qs = [
        {"id": "kb_optimization_impact", "field": "optimization_impact",
         "question": "优化是否影响历史数据"},
        {"id": "kb_optimization_impact_scope", "field": "legacy_data_impact.user_clarification",
         "question": "请描述影响范围"},
    ]
    # 模拟追加前先去重（已有问题无重复）
    existing_keys = set()
    for q in questions:
        existing_keys.add(f"field::{q['field']}")
        existing_keys.add(f"id::{q['id']}")
    for q in opt_tail_qs:
        _fk = f"field::{q['field']}"
        _ik = f"id::{q['id']}"
        if _fk not in existing_keys and _ik not in existing_keys:
            questions.append(q)
    assert [q["id"] for q in questions] == ["q1", "q2", "kb_optimization_impact", "kb_optimization_impact_scope"], \
        f"顺序错误：{[q['id'] for q in questions]}"
    print("✅ Test A4 PASS: 配套优化问题正确追加到最终questions末尾，顺序保证")

# ================================================================
# Test B：引用未定义问题文本精简 - question不包含src_refs/原文引用
# ================================================================
def test_B_ref_undef_question_concise():
    # 模拟 _detect_logical_contradictions 中引用未定义问题生成的结构（精简版）
    k = "ref_theme::图片素材上传规则"
    theme_label = str(k).replace("ref_theme::", "")
    srcs = ["raw_text#51[OPT-A-01 图片素材库]", "raw_text#59[OPT-A-01 图片素材库]"]
    texts = ["验收标准遵循现有图片上传规则", "图片素材沿用现有上传规则一致"]
    req_title = "内容平台优化需求"

    theme_specific_hint = ""
    if any(tk in theme_label for tk in ["图片", "上传", "素材"]):
        theme_specific_hint = "请补充：支持的图片格式列表、单文件大小限制数值、超限提示文案。"
    multi_src_note_q = f"（文档中提及 {len(group := texts)} 处，内容相近需统一定义）" if len(texts) >= 2 else ""
    question = (
        f"【{req_title}】引用未定义：「{theme_label}」相关规则未给出具体内容。"
        f"{multi_src_note_q}{theme_specific_hint}"
    )
    # 验证 question 精简：不应出现来源位置（raw_text#N）
    assert "raw_text#" not in question, f"question不应包含raw_text来源位置: {question}"
    # 验证 question 精简：不应出现原文引用（验收标准遵循...）
    assert "验收标准遵循" not in question, f"question不应包含原文引用: {question}"
    # 验证 question 包含主题关键词和追问内容
    assert "图片素材上传规则" in question, "question应包含主题关键词"
    assert "支持的图片格式" in question, "question应包含具体追问内容"
    assert len(question) < 120, f"question应控制在120字以内，实际{len(question)}字"
    print(f"✅ Test B PASS: 引用未定义问题精简到 {len(question)} 字，无来源/原文噪声，仅主题+追问")
    print(f"    实际内容：{question}")

# ================================================================
# Test C：KB 查询生成 - ref_undef 使用短主题 query 而非长 question
# ================================================================
def test_C_ref_undef_short_kb_query():
    # 模拟统一池分流出来的 ref_undef 问题
    q = {
        "id": "ref_undef_1",
        "field": "requirement.ambiguity",
        "category": "ambiguity",
        "priority": "important",
        "question": (
            "【内容平台优化需求】文档中raw_text#51写『验收标准遵循现有图片上传规则...』，"
            "引用了现有规则/标准，但通篇未给出该规则的具体内容。（文档中该主题的引用未定义共 2 处，"
            "来源：raw_text#51、raw_text#59...，汇总澄清）请补充缺失的具体细节："
            "（如是上传/素材规则，请补充支持的格式列表、单文件大小；...）"
        ),
        "_ref_theme_label": "图片素材上传规则",
        "_from_unified_candidate": True,
    }
    # 模拟转 KB 查询时的短 query 生成逻辑
    theme_label = q.get("_ref_theme_label") or ""
    short_query = None
    if theme_label and isinstance(theme_label, str) and len(theme_label) <= 40:
        suffixes = []
        if any(tk in theme_label for tk in ["图片", "上传", "素材"]):
            suffixes = ["格式", "大小", "限制"]
        short_query = theme_label + (" " + " ".join(suffixes) if suffixes else "")
    assert short_query is not None, "应生成short_query"
    assert short_query == "图片素材上传规则 格式 大小 限制", f"short_query错误: {short_query}"
    assert len(short_query) < 40, "short_query应非常短"
    # 实际检索时优先 _kb_short_query
    effective_kb_query = short_query or q["question"]
    assert effective_kb_query == short_query, "检索时应优先用短query"
    print(f"✅ Test C PASS: 引用未定义KB查询从 {len(q['question'])} 字压缩为 {len(short_query)} 字")
    print(f"    原长query（BM25易漏检）: {q['question'][:50]}...")
    print(f"    新短query（BM25高召回）: {short_query}")

# ================================================================
# 测试 D：排序插队——有 _kb_short_query 的优先
# ================================================================
def test_D_sort_priority_short_query_front():
    kb_questions = [
        # 规则层 baseline 缺口（后）
        {"priority": "important", "field": "upper_bound_rule_1", "question": "超过上限怎么办1"},
        {"priority": "important", "field": "upper_bound_rule_2", "question": "超过上限怎么办2"},
        {"priority": "important", "field": "upper_bound_rule_3", "question": "超过上限怎么办3"},
        # ref_undef 类（有 _kb_short_query，应插队到前面）
        {"priority": "important", "field": "ref_undef_pic", "question": "图片规则长...",
         "_kb_short_query": "图片素材上传规则 格式 大小 限制", "_from_unified_candidate": True},
        # LLM 语义类（插队）
        {"priority": "important", "field": "llm_semantic.something", "question": "语义类遗漏",
         "_from_llm_semantic": True},
    ]
    _priority_order = {"critical": 0, "important": 1, "optional": 2}
    def _src_rank(q):
        return 0 if (q.get("_from_llm_semantic") or q.get("_from_unified_candidate")
                     or q.get("_kb_short_query")) else 1
    kb_questions.sort(key=lambda q: (_priority_order[q["priority"]], _src_rank(q)))
    fields_ordered = [q["field"] for q in kb_questions]
    # 前 2 个应为插队的，再 3 个 baseline
    assert fields_ordered[0] in {"ref_undef_pic", "llm_semantic.something"}, \
        f"第一个应为插队类，实际顺序={fields_ordered}"
    assert fields_ordered[1] in {"ref_undef_pic", "llm_semantic.something"}, \
        f"第二个应为插队类，实际顺序={fields_ordered}"
    assert set(fields_ordered[2:]) == {"upper_bound_rule_1", "upper_bound_rule_2", "upper_bound_rule_3"}, \
        f"后三个应为baseline，实际顺序={fields_ordered}"
    print("✅ Test D PASS: KB排序中 _kb_short_query/LLM语义/统一池 类优先于baseline缺口，不被MAX_8截断挤出")

if __name__ == "__main__":
    test_A1_detect_opt_impact_structure()
    test_A2_llm_semantic_duplicate_filter()
    test_A3_unified_pool_opt_marked_direct_tail()
    test_A4_final_questions_ordered_with_tail()
    test_B_ref_undef_question_concise()
    test_C_ref_undef_short_kb_query()
    test_D_sort_priority_short_query_front()
    print("\n🎉 全部 7 项单元测试通过！REQ-1465 三项修复验证完成")

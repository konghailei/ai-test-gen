"""v2.31 去重逻辑单元测试。

验证多层去重机制：
1. _generate_kb_queries 源头去重（LLM 返回重复查询时过滤）
2. _ensure_critical_questions 结构统一 + 双重去重
3. _dedup_questions 全文去重安全网
4. _detect_optimization_impact 优化需求检测
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from app.agents.clarify_agent import ClarifyAgent, _build_field_question


def test_dedup_questions_basic():
    """测试 _dedup_questions 基本去重。"""
    print("=" * 60)
    print("测试1: _dedup_questions 基本去重")
    print("=" * 60)

    questions = [
        {"id": "kb_data_constraints", "field": "data_constraints", "question": "文件大小限制是多少？"},
        {"id": "kb_data_constraints", "field": "data_constraints", "question": "文件大小限制是多少？"},  # 完全重复
        {"id": "kb_data_constraints_2", "field": "data_constraints", "question": "图片格式限制是什么？"},  # 同field不同question
    ]

    result = ClarifyAgent._dedup_questions(questions, stage="test1")
    assert len(result) == 2, f"期望2条（去掉1条完全重复），实际{len(result)}条"
    assert result[0]["id"] == "kb_data_constraints"
    assert result[1]["id"] == "kb_data_constraints_2"
    print(f"  ✅ 通过: {len(questions)}条 → {len(result)}条（正确去重完全重复，保留同field不同question）")


def test_dedup_questions_id_only():
    """测试 id 相同但 question 不同时按 id 去重。"""
    print("\n" + "=" * 60)
    print("测试2: id 相同但 question 不同时按 id 去重")
    print("=" * 60)

    questions = [
        {"id": "kb_test", "field": "test_field", "question": "问题A？"},
        {"id": "kb_test", "field": "test_field", "question": "问题B？"},  # id 相同但 question 不同
    ]

    result = ClarifyAgent._dedup_questions(questions, stage="test2")
    assert len(result) == 1, f"期望1条（id重复），实际{len(result)}条"
    print(f"  ✅ 通过: id 相同时按 id 去重 → {len(result)}条")


def test_dedup_questions_empty():
    """测试空列表。"""
    print("\n" + "=" * 60)
    print("测试3: 空列表")
    print("=" * 60)

    result = ClarifyAgent._dedup_questions([], stage="test3")
    assert result == []
    print(f"  ✅ 通过: 空列表返回空列表")


def test_dedup_questions_no_id():
    """测试无 id 的问题去重。"""
    print("\n" + "=" * 60)
    print("测试4: 无 id 的问题按 field+question 去重")
    print("=" * 60)

    questions = [
        {"field": "test", "question": "无id问题？"},
        {"field": "test", "question": "无id问题？"},  # 完全重复
        {"field": "test", "question": "另一个无id问题？"},  # 不同question
    ]

    result = ClarifyAgent._dedup_questions(questions, stage="test4")
    assert len(result) == 2, f"期望2条，实际{len(result)}条"
    print(f"  ✅ 通过: 无 id 时按 field+question 去重 → {len(result)}条")


def test_ensure_critical_questions_structure():
    """测试 _ensure_critical_questions 追加问题的结构一致性。"""
    print("\n" + "=" * 60)
    print("测试5: _ensure_critical_questions 结构统一")
    print("=" * 60)

    agent = ClarifyAgent()

    # 模拟 llm_questions（已有 summary 问题，但没有 acceptance_criteria）
    llm_questions = [
        {
            "id": "kb_requirement_summary",
            "field": "requirement.summary",
            "priority": "critical",
            "category": "completeness",
            "question": "功能概述是什么？",
            "context": "...",
            "default_answer": "",
            "input_type": "text",
            "options": [],
        }
    ]

    # 模拟 unresolved_qa（包含 acceptance_criteria 的未答问题）
    unresolved_qa = [
        {
            "id": "q_acceptance_criteria",  # 原始 qa 的 id 格式
            "field": "acceptance_criteria",
            "question": "验收标准是什么？",
            "missing_desc": "验收标准",
            "priority": "critical",
            "answered": False,
        }
    ]

    completeness = {
        "critical_missing": [
            {"field": "requirement.summary", "description": "功能概述"},
            {"field": "acceptance_criteria", "description": "验收标准"},
        ],
        "important_missing": [],
    }

    srr = {
        "requirement": {"title": "测试需求", "summary": "这是一个测试需求"},
        "fp_summary": [],
    }

    result = agent._ensure_critical_questions(llm_questions, unresolved_qa, completeness, srr=srr)

    # 应该追加 acceptance_criteria 问题
    assert len(result) == 2, f"期望2条，实际{len(result)}条"

    # 检查追加的问题结构
    appended = result[1]
    print(f"  追加的问题: id={appended['id']} field={appended['field']} category={appended.get('category')}")

    # v2.31: id 应该是 kb_ 前缀（统一格式），不是 q_ 前缀
    assert appended["id"] == "kb_acceptance_criteria", f"id 应为 kb_acceptance_criteria，实际 {appended['id']}"
    assert appended["category"] == "completeness", f"category 应为 completeness，实际 {appended.get('category')}"
    assert "context" in appended, "缺少 context 字段"
    assert "input_type" in appended, "缺少 input_type 字段"
    assert "options" in appended, "缺少 options 字段"
    print(f"  ✅ 通过: 追加问题结构统一（id=kb_前缀, category=completeness, 含所有必需字段）")


def test_ensure_critical_questions_dedup():
    """测试 _ensure_critical_questions 不会追加重复问题。"""
    print("\n" + "=" * 60)
    print("测试6: _ensure_critical_questions 双重去重")
    print("=" * 60)

    agent = ClarifyAgent()

    # llm_questions 已有 acceptance_criteria 问题
    llm_questions = [
        {
            "id": "kb_acceptance_criteria",
            "field": "acceptance_criteria",
            "question": "验收标准是什么？",
            "priority": "critical",
            "category": "completeness",
        }
    ]

    # unresolved_qa 也有 acceptance_criteria 的 qa（question 相同）
    unresolved_qa = [
        {
            "field": "acceptance_criteria",
            "question": "验收标准是什么？",  # 与 llm_questions 相同
            "missing_desc": "验收标准",
            "priority": "critical",
        }
    ]

    completeness = {
        "critical_missing": [
            {"field": "acceptance_criteria", "description": "验收标准"},
        ],
    }

    result = agent._ensure_critical_questions(llm_questions, unresolved_qa, completeness, srr={})

    # 因为 llm_questions 已有 acceptance_criteria field，不会再追加
    assert len(result) == 1, f"期望1条（不重复追加），实际{len(result)}条"
    print(f"  ✅ 通过: llm_questions 已有该 field 时不重复追加 → {len(result)}条")


def test_generate_kb_queries_source_dedup():
    """测试 _generate_kb_queries 源头去重（模拟 LLM 返回重复查询）。"""
    print("\n" + "=" * 60)
    print("测试7: _generate_kb_queries 源头去重逻辑验证")
    print("=" * 60)

    # 这个测试验证源头去重的逻辑正确性（不调用 LLM，直接模拟 validated 列表构建）
    # 模拟 LLM 返回的重复查询
    queries = [
        {"field": "data_constraints", "question": "文件大小限制是多少？", "priority": "important"},
        {"field": "data_constraints", "question": "文件大小限制是多少？", "priority": "important"},  # 完全重复
        {"field": "data_constraints", "question": "图片格式限制是什么？", "priority": "important"},  # 同field不同question
    ]

    # 模拟源头去重逻辑
    valid_fields = {"data_constraints"}
    validated = []
    seen_query_keys = set()
    for q in queries:
        field = q.get("field", "")
        if field and field not in valid_fields:
            continue
        q_text = q["question"][:40]
        query_dedup_key = f"{field}::{q_text}"
        if query_dedup_key in seen_query_keys:
            continue
        seen_query_keys.add(query_dedup_key)
        validated.append(q)

    assert len(validated) == 2, f"期望2条（源头去重1条），实际{len(validated)}条"
    assert validated[0]["question"] == "文件大小限制是多少？"
    assert validated[1]["question"] == "图片格式限制是什么？"
    print(f"  ✅ 通过: 源头去重 {len(queries)}条 → {len(validated)}条（去掉完全重复，保留同field不同question）")


def test_full_pipeline_dedup_simulation():
    """模拟完整流水线的去重效果。"""
    print("\n" + "=" * 60)
    print("测试8: 完整流水线去重模拟")
    print("=" * 60)

    # 模拟 KB 未答问题（包含重复）
    unresolved_qa = [
        {"field": "data_constraints", "question": "文件大小限制是多少？", "missing_desc": "数据约束", "priority": "important", "answered": False},
        {"field": "data_constraints", "question": "文件大小限制是多少？", "missing_desc": "数据约束", "priority": "important", "answered": False},  # 完全重复
        {"field": "data_constraints", "question": "图片格式限制是什么？", "missing_desc": "数据约束", "priority": "important", "answered": False},  # 同field不同question
        {"field": "business_rules", "question": "权限控制规则是什么？", "missing_desc": "业务规则", "priority": "important", "answered": False},
    ]

    # 步骤1: 构建 questions 时的去重（模拟 clarify 方法中的逻辑）
    seen_keys = set()
    field_counters = {}
    questions = []
    for qa in unresolved_qa:
        field = qa.get("field", "")
        question_text = qa.get("question", "") or ""
        dedup_key = f"{field}::{question_text[:40]}" if question_text else field
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        field_counters[field] = field_counters.get(field, 0) + 1
        field_slug = field.replace('.', '_') if field else "gap"
        if field_counters[field] == 1:
            qid = f"kb_{field_slug}"
        else:
            qid = f"kb_{field_slug}_{field_counters[field]}"
        questions.append({"id": qid, "field": field, "question": question_text})

    print(f"  步骤1 去重后: {len(unresolved_qa)}条 → {len(questions)}条")
    for q in questions:
        print(f"    - id={q['id']} field={q['field']} q={q['question'][:40]}")

    assert len(questions) == 3, f"步骤1去重后期望3条，实际{len(questions)}条"

    # 步骤2: 模拟 _ensure_critical_questions（不会追加，因为 critical_fields 为空）
    # 步骤3: 模拟 _refine_unresolved_to_questions（不增加数量）
    # 步骤4: 模拟优化需求检测（追加1条）
    opt_q = {"id": "kb_optimization_impact", "field": "optimization_impact", "question": "是否影响历史数据？"}
    questions.append(opt_q)

    # 步骤5: 最终全文去重
    final_questions = ClarifyAgent._dedup_questions(questions, stage="final")
    print(f"\n  最终去重后: {len(questions)}条 → {len(final_questions)}条")
    for q in final_questions:
        print(f"    - id={q['id']} field={q['field']} q={q['question'][:40]}")

    assert len(final_questions) == 4, f"最终去重后期望4条，实际{len(final_questions)}条"

    # 检查 id 唯一性
    ids = [q["id"] for q in final_questions]
    assert len(ids) == len(set(ids)), f"id 不唯一: {ids}"

    # 检查 field+question 唯一性
    q_keys = [f"{q['field']}::{q['question']}" for q in final_questions]
    assert len(q_keys) == len(set(q_keys)), f"field+question 不唯一: {q_keys}"

    print(f"\n  ✅ 通过: 最终 {len(final_questions)} 条问题，id 唯一、field+question 唯一")


def test_detect_optimization_impact():
    """测试优化需求检测。"""
    print("\n" + "=" * 60)
    print("测试9: 优化需求检测")
    print("=" * 60)

    agent = ClarifyAgent()

    # 包含优化关键词
    opt_srr = {
        "requirement": {
            "title": "白板性能优化",
            "summary": "优化白板渲染性能，提升用户体验",
        },
        "_raw_text": "本次需求是对白板渲染引擎进行优化，提升大画布的渲染性能...",
    }
    opt_q = agent._detect_optimization_impact(opt_srr)
    assert opt_q is not None, "包含'优化'关键词应返回问题"
    assert opt_q["id"] == "kb_optimization_impact"
    assert opt_q["input_type"] == "multi_select"
    assert len(opt_q["options"]) >= 4
    print(f"  ✅ 通过: 检测到优化需求，生成历史数据影响问题（{len(opt_q['options'])}个选项）")

    # 不包含优化关键词
    normal_srr = {
        "requirement": {
            "title": "用户登录功能",
            "summary": "实现用户账号密码登录",
        },
        "_raw_text": "本次需求是实现用户登录功能...",
    }
    normal_q = agent._detect_optimization_impact(normal_srr)
    assert normal_q is None, "不包含优化关键词应返回 None"
    print(f"  ✅ 通过: 非优化需求不生成历史数据影响问题")


def main():
    print("\n" + "=" * 60)
    print("v2.31 去重逻辑单元测试")
    print("=" * 60)

    try:
        test_dedup_questions_basic()
        test_dedup_questions_id_only()
        test_dedup_questions_empty()
        test_dedup_questions_no_id()
        test_ensure_critical_questions_structure()
        test_ensure_critical_questions_dedup()
        test_generate_kb_queries_source_dedup()
        test_full_pipeline_dedup_simulation()
        test_detect_optimization_impact()

        print("\n" + "=" * 60)
        print("✅ 所有测试通过！")
        print("=" * 60)
        return 0
    except AssertionError as e:
        print(f"\n❌ 测试失败: {e}")
        return 1
    except Exception as e:
        print(f"\n❌ 测试异常: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())

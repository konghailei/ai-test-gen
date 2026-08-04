"""测试 ClarifyAgent 去重逻辑是否生效。"""
import asyncio
import sys
import os

sys.path.insert(0, "/app")

from app.agents.clarify_agent import ClarifyAgent


async def main():
    agent = ClarifyAgent()

    # 模拟 _query_kb_for_answers 返回的 kb_answers（2条 field="data_constraints" 的未答问题）
    kb_answers = [
        {
            "id": "q_data_constraints",
            "field": "data_constraints",
            "missing_desc": "数据约束/校验规则",
            "context": "数据约束/校验规则",
            "priority": "important",
            "question": "便签和文本框输入的文本最大长度限制是多少？",
            "answer": "知识库中未找到相关信息，需要人工补充",
            "answered": False,
            "sources": [],
            "auto_filled": False,
            "input_type": "text",
            "options": [],
            "default_answer": "数据约束/校验规则",
            "duration_ms": 100,
        },
        {
            "id": "q_data_constraints",
            "field": "data_constraints",
            "missing_desc": "数据约束/校验规则",
            "context": "数据约束/校验规则",
            "priority": "important",
            "question": "向白板导入图片和PDF文件时，单个文件的最大大小限制是多少？",
            "answer": "知识库中未找到相关信息，需要人工补充",
            "answered": False,
            "sources": [],
            "auto_filled": False,
            "input_type": "text",
            "options": [],
            "default_answer": "数据约束/校验规则",
            "duration_ms": 100,
        },
    ]

    # 模拟步骤⑧的去重逻辑
    unresolved_qa = [qa for qa in kb_answers if not qa.get("answered")]
    print(f"unresolved_qa 数量: {len(unresolved_qa)}")
    for i, qa in enumerate(unresolved_qa):
        print(f"  [{i}] field={qa.get('field')!r} question={qa.get('question')!r}")

    seen_fields: set[str] = set()
    questions = []
    augmented_srr = {
        "requirement": {"title": "测试需求", "summary": "测试摘要"},
        "fp_summary": [],
    }
    for qa in unresolved_qa:
        field = qa.get("field", "")
        dedup_key = field or qa.get("question", "")[:50]
        print(f"  处理 qa: field={field!r} dedup_key={dedup_key!r} seen_fields={seen_fields}")
        if dedup_key in seen_fields:
            print(f"  ✅ 跳过重复: field={field!r}")
            continue
        seen_fields.add(dedup_key)
        from app.agents.clarify_agent import _build_field_question
        desc = qa.get("missing_desc", field)
        q_data = _build_field_question(field, desc, augmented_srr)
        questions.append({
            "id": f"kb_{field.replace('.','_')}" if field else "kb_gap",
            "field": field,
            "priority": qa.get("priority", "important"),
            "category": "completeness",
            "question": qa.get("question", "") or q_data["question"],
            "context": q_data["context"],
            "default_answer": qa.get("missing_desc", ""),
            "input_type": q_data["input_type"],
            "options": q_data["options"],
        })

    print(f"\n去重后 questions 数量: {len(questions)}")
    for i, q in enumerate(questions):
        print(f"  [{i}] id={q['id']} field={q['field']} question={q['question'][:60]}")


if __name__ == "__main__":
    asyncio.run(main())

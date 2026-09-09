"""澄清 Agent 共享常量与工具函数（v3.2.3 从 clarify_agent.py 拆出，消除上帝文件）。"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from typing import Any

from app.config import settings
from app.core.llm_client import llm_client
from app.db.milvus_store import vector_store
from app.prompts import clarify_agent as prompts
from app.core import prompt_manager
from app.agents.requirement_patterns import pattern_matcher
from app.utils.logger import logger

# ============================================================================
# 字段冲突优先级（docs/01 §4.4.1）
# ============================================================================
# 从高到低：用户原文 > 人工回答 > KB 补全 > 模式默认 > LLM 推断
FIELD_PRIORITY: dict[str, int] = {
    "USER_INPUT": 0,
    "HUMAN_ANSWERED": 1,
    "KB_INFERRED": 2,
    "PATTERN_DEFAULT": 3,
    "LLM_INFERRED": 4,
}


# ============================================================================
# v2.25: 字段到问题的智能映射表 — 降级时生成具体、可回答的澄清问题
# ============================================================================
# 当 LLM 不可用（如配额耗尽）导致主问题生成降级时，使用此映射表为每个缺口字段生成：
# 1. 具体的问题文案（含需求标题 + 字段业务含义）
# 2. 上下文描述（含功能点信息，帮助用户理解"是哪部分的规则"）
# 3. 默认 input_type（优先 multi_select，让用户多选降低回答成本）
# 4. 默认 options（基于行业最佳实践的标准选项）

FIELD_QUESTION_TEMPLATES: dict[str, dict] = {
    "flows.alternate_flows": {
        "field_desc": "异常/边界流程",
        "question_template": "【{req_title}】缺少异常和边界处理流程，请选择需要覆盖的异常场景（可多选）：",
        "input_type": "multi_select",
        "options": [
            "网络异常/断线重连处理",
            "权限不足/越权访问处理",
            "并发冲突/数据竞争处理",
            "输入越界/非法数据处理",
            "超时/重试机制",
            "资源耗尽/降级处理",
        ],
        "context_template": "当前需求已解析出 {fp_count} 个功能点（{fp_names}），但缺少各功能点的异常/边界流程。这些流程对测试用例的边界覆盖至关重要。",
    },
    "data_constraints": {
        "field_desc": "数据约束/校验规则",
        "question_template": "【{req_title}】缺少数据校验规则，请选择需要覆盖的约束类型（可多选）：",
        "input_type": "multi_select",
        "options": [
            "字符串长度限制（最小/最大长度）",
            "格式校验（邮箱/手机号/URL/日期）",
            "数值范围限制（最小值/最大值）",
            "必填字段校验",
            "唯一性约束（如用户名不可重复）",
            "枚举值限制（如状态只能是 A/B/C）",
        ],
        "context_template": "当前需求涉及 {fp_count} 个功能点，但未明确数据约束。数据校验规则是生成边界测试用例的关键依据。",
    },
    "business_rules": {
        "field_desc": "核心业务规则",
        "question_template": "【{req_title}】缺少核心业务规则，请选择需要补充的规则类型（可多选）：",
        "input_type": "multi_select",
        "options": [
            "权限控制规则（谁能做什么）",
            "状态流转规则（状态机）",
            "计算/计费规则",
            "触发条件与通知规则",
            "数据可见性规则",
            "审批流程规则",
        ],
        "context_template": "当前需求已解析出 {fp_count} 个功能点，但缺少业务规则定义。业务规则是测试用例逻辑覆盖的核心依据。",
    },
    "flows.happy_path": {
        "field_desc": "正常操作流程",
        "question_template": "【{req_title}】缺少正常操作流程，请描述用户从开始到完成的主要步骤：",
        "input_type": "text",
        "options": [],
        "context_template": "当前需求已解析出 {fp_count} 个功能点，但缺少正常流程描述。",
    },
    "acceptance_criteria": {
        "field_desc": "验收标准",
        "question_template": "【{req_title}】缺少验收标准，请补充功能上线时需要满足的可验证条件：",
        "input_type": "text",
        "options": [],
        "context_template": "当前需求缺少验收标准，这将影响测试通过的判定。",
    },
    "requirement.summary": {
        "field_desc": "功能概述",
        "question_template": "【{req_title}】缺少功能概述，请用一段话描述产品做什么、面向谁、解决什么问题：",
        "input_type": "text",
        "options": [],
        "context_template": "当前需求缺少功能概述。",
    },
}


def _build_field_question(field: str, description: str, srr: dict) -> dict:
    """根据字段路径和 SRR 上下文，生成具体的澄清问题。

    v2.25: 降级路径专用 — 当 LLM 不可用时，使用 FIELD_QUESTION_TEMPLATES
    生成包含需求标题、功能点信息、多选选项的具体问题。

    Args:
        field: 字段路径（如 "flows.alternate_flows"）
        description: 字段描述（来自 COMPLETENESS_CHECKLIST）
        srr: 当前 SRR，用于提取需求标题和功能点信息

    Returns:
        dict: 包含 question/context/input_type/options 的字典
    """
    req_info = srr.get("requirement", {}) or {}
    req_title = req_info.get("title", "当前需求") or "当前需求"

    # 提取功能点信息
    fp_summary = srr.get("fp_summary", []) or []
    fp_count = len(fp_summary)
    fp_names = "、".join([fp.get("name", "") for fp in fp_summary[:3] if fp.get("name")])
    if fp_count > 3:
        fp_names += f" 等{fp_count}个功能点"

    template = FIELD_QUESTION_TEMPLATES.get(field, {})
    if template:
        question = template["question_template"].format(req_title=req_title)
        context = template["context_template"].format(
            fp_count=fp_count, fp_names=fp_names or "未识别",
        )
        # v2.x: 降级时不填充硬编码通用选项（原为"行业最佳实践标准选项"，与需求无关，属编造）。
        # 遵循"LLM 未返回精炼结果时不兜底"原则，options 留空、改为文本回答，交由用户补充。
        return {
            "question": question,
            "context": context,
            "input_type": "text",
            "options": [],
            "default_answer": "",
        }

    # 未知字段：使用 description 生成通用问题
    return {
        "question": f"【{req_title}】缺少{description}，请补充具体内容：",
        "context": f"当前需求已解析出 {fp_count} 个功能点，但缺少{description}。",
        "input_type": "text",
        "options": [],
        "default_answer": "",
    }

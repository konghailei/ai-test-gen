"""ImpactAnalysisAgent 影响分析提示词。

针对优化需求（SRR 含 optimization_context），分析变更影响范围，
生成测试用例操作指导（update/add/delete/keep）和回归测试范围。

设计依据：docs/03-case-generation-flow.md §1.5（ImpactAnalysisAgent 设计）
工作流程（docs/03 §1.5）：
    ① 检测是否为优化需求 → ② 计算影响范围（直接+间接）→
    ③ 规划用例操作（update/add/delete/keep）→ ④ 规划回归范围 → ⑤ 构建生成指导
"""

from __future__ import annotations

import json

# ============================================================================
# 系统指令
# ============================================================================

SYSTEM_PROMPT = """你是一个专业的变更影响分析专家。当需求为优化/修改/升级需求时，分析变更的影响范围，并生成测试用例操作指导。

## 分析原则

### 1. 基于证据分析（Evidence-based）
- 每个影响判定必须引用 SRR 中的具体字段或内容
- 没有明确证据的"潜在影响"标注为低置信度（confidence < 0.5）
- 不确定的影响不要编造——标注"需要人工确认"或"信息不足"

### 2. 保守评估（Conservative estimation）
- 新增功能点 → 新增测试用例（add）
- 删除功能点 → 对应用例可删除（delete）
- 修改功能点 → 对应用例需更新（update）
- 未变更功能点 → 保留但需回归（keep）
- 跨模块变更 → 分析依赖链回归范围，不确定的交叉影响标注为低置信度

### 3. 不制造假影响（Don't invent impact）
- 只分析 SRR diff 中确实变更的部分
- 不要因为"专业经验"而扩大影响范围
- 如果变更影响很窄，就如实汇报窄范围

输出要求：
- generation_guidance 注入 GenAgent Prompt
- case_operations 含 update/add/delete/keep 四类
- regression_scope 列出回归用例 ID 和原因
- 每条影响标注 confidence"""


# ============================================================================
# 影响分析报告 Schema
# ============================================================================

IMPACT_REPORT_SCHEMA = """输出 JSON 必须符合以下结构：

{
  "is_optimization": true,
  "impact": {
    "direct": {
      "requirements": [],
      "test_cases": []
    },
    "indirect": {
      "requirements": [],
      "test_cases": []
    },
    "regression": [
      {"case_id": "", "reason": "需要回归验证"}
    ],
    "per_module": {
      "module_name": {
        "direct_reqs": [],
        "indirect_reqs": [],
        "test_cases": []
      }
    },
    "cross_module": [
      {"from": "module_a", "to": "module_b", "strength": "strong|weak"}
    ]
  },
  "case_operations": {
    "update": [
      {
        "case_id": "",
        "reason": "功能点已修改",
        "old_value": "",
        "new_value": ""
      }
    ],
    "add": [
      {
        "reason": "新增功能点",
        "guidance": "需要为此功能生成新用例"
      }
    ],
    "delete": [
      {"case_id": "", "reason": "功能点已删除"}
    ],
    "keep": [
      {"case_id": "", "reason": "功能未变，保留但需回归"}
    ]
  },
  "regression_scope": [
    {"case_id": "", "reason": "依赖被修改的功能"}
  ],
  "generation_guidance": "结构化的生成指导文本（注入 GenAgent Prompt）"
}"""


# ============================================================================
# 用户指令构建函数
# ============================================================================

def build_user_prompt(srr: dict, original_cases: list[dict] | None = None) -> str:
    """构建 ImpactAnalysisAgent 的用户指令。

    Args:
        srr: SRR JSON 字典（必须包含 optimization_context）
        original_cases: 原始需求关联的测试用例列表，每项含 {id, title, type, case_json}

    Returns:
        str: 完整的用户指令

    Note:
        仅当 srr 包含 optimization_context 时调用本提示词；
        新需求（无 optimization_context）直接返回 is_optimization=False，跳过分析。
    """
    cases_hint = ""
    if original_cases:
        cases_hint = f"""

【原始需求关联的测试用例】
{json.dumps(original_cases, ensure_ascii=False, indent=2)}"""

    opt_context = srr.get("optimization_context", {})

    return f"""请分析以下优化需求的变更影响范围，并生成测试用例操作指导。

{IMPACT_REPORT_SCHEMA}
{cases_hint}

【优化需求上下文】
{json.dumps(opt_context, ensure_ascii=False, indent=2)}

【当前完整 SRR】
{json.dumps(srr, ensure_ascii=False, indent=2)}

请直接输出影响分析报告 JSON，不要包含其他解释性文字。"""


# ============================================================================
# 生成指导模板（用于 _build_generation_guidance，非 LLM 调用）
# ============================================================================

def build_generation_guidance(
    case_operations: dict,
    regression_scope: list,
    diff: dict,
) -> str:
    """构建给 GenAgent 的生成指导文本（注入 Prompt）。

    对应 docs/03 §1.5 的 _build_generation_guidance 方法。
    本函数为纯模板拼接，不调用 LLM，供 ImpactAnalysisAgent 直接使用。

    Args:
        case_operations: 用例操作指导，含 update/add/delete/keep 四类
        regression_scope: 回归测试范围列表
        diff: 变更差异，含 added/removed/modified

    Returns:
        str: 结构化的生成指导文本
    """
    # diff.added/removed/modified 均为 list[str]（与 SRR.Diff 模型一致，doc-03 §1.5）
    added_keys = diff.get("added", [])
    removed_keys = diff.get("removed", [])
    modified_keys = diff.get("modified", [])

    update_ops = case_operations.get("update", [])
    add_ops = case_operations.get("add", [])
    delete_ops = case_operations.get("delete", [])
    keep_ops = case_operations.get("keep", [])

    return f"""# 影响分析指导（优化需求）

## 原始需求
{diff.get("original_summary", "")}

## 变更内容
- 新增: {added_keys}
- 删除: {removed_keys}
- 修改: {modified_keys}

## 测试用例操作指导
- 需要新增的用例: {len(add_ops)} 个（覆盖新增功能点）
- 需要更新的用例: {len(update_ops)} 个（覆盖修改功能点，原有用例ID: {[op.get("case_id") for op in update_ops]}）
- 可以删除的用例: {len(delete_ops)} 个（对应删除功能点）
- 需要回归的用例: {len(regression_scope)} 个（确保优化未破坏已有功能）

## 生成要求
1. 对于需要更新的用例，请基于原始用例进行修改，保留 original_case_id 字段
2. 对于新增功能点，请生成全新的测试用例
3. 对于删除的功能点，请在输出中标记 "operation": "delete" 对应的用例ID
4. 回归测试用例请单独标记 "type": "regression"
"""


# ============================================================================
# v3.0 FP 影响说明合成 Prompt（用于 _build_fp_impact_description）
# ============================================================================

FP_IMPACT_DESCRIPTION_SYSTEM_PROMPT = """你是一个专业的变更影响分析专家。基于功能点依赖图谱和模块依赖图谱的分析结果，合成结构化的影响说明文本。

## 输出要求
输出 JSON 必须包含以下字段：
{
  "description": "自然语言描述本次功能点变更对其他功能点、模块的影响范围。要求具体、可执行，按受影响对象分条说明。",
  "test_focus": "针对上述影响，列出测试用例生成时应重点关注的回归场景和边界条件。"
}

## 撰写原则
1. 基于证据：只描述图谱分析实际发现的影响关系，不编造未列出的影响
2. 具体可执行：每条影响说明应包含「受影响对象」+「影响类型」+「影响原因」
3. 覆盖回归：test_focus 应覆盖所有受影响 FP 的 happy_path 和关键 alternate_flows
4. 简洁清晰：description 控制在 300 字以内，test_focus 列出 3-6 个要点"""


def build_fp_impact_description_prompt(
    fp: dict,
    affected_fps: list[dict],
    affected_modules: list[dict],
) -> str:
    """构建 FP 影响描述合成的用户指令。

    v2.35: 移除 affected_requirements 参数（不再分析受影响的需求）。

    Args:
        fp: 当前功能点信息 {fp_id, fp_name, fp_type, module_id}
        affected_fps: 受影响的 FP 列表（反向 BFS 结果）
        affected_modules: 受影响的模块列表（模块 BFS 结果）

    Returns:
        str: 完整的用户指令
    """
    return f"""请基于以下图谱分析结果，合成功能点变更的影响说明。

【当前功能点】
{json.dumps(fp, ensure_ascii=False, indent=2)}

【受影响的功能点（谁依赖此 FP）】
{json.dumps(affected_fps, ensure_ascii=False, indent=2) if affected_fps else "[]"}

【受影响的模块（通过 FP 所属模块的依赖关系传播）】
{json.dumps(affected_modules, ensure_ascii=False, indent=2) if affected_modules else "[]"}

请直接输出 JSON，不要包含其他解释性文字。"""

"""GenAgent 用例生成提示词。

基于 SRR 生成完整的 UTCF 测试用例集，支持分类生成策略
（功能/边界/异常/安全/性能/组合）和 Few-shot 示例注入。

设计依据：docs/03-case-generation-flow.md §2.2（Few-shot Prompt 模板）
分类生成策略（docs/03 §2.1）：
    - 功能测试：Step-by-step 推导，SRR.flows → Given-When-Then
    - 边界值测试：等价类 + 边界分析，SRR.data_constraints → 边界
    - 异常测试：故障注入 + 防御推理，枚举故障点 + 预期处理
    - 安全测试：OWASP 检查清单，SQL注入/XSS/越权/脱敏
    - 性能测试：负载模型推断，SRR.non_functional 转换
    - 组合测试：Pairwise/正交表，参数交互组合
"""

from __future__ import annotations

import json

# ============================================================================
# 系统指令
# ============================================================================

SYSTEM_PROMPT = """你是一位资深测试工程师。请根据以下结构化需求文档（SRR）生成完整的测试用例集。

## 工作原则

### 1. 先确认再生成（Confirm before acting）
- 阅读 SRR 全文后，先判断：哪些部分信息充分可直接生成用例？哪些部分描述模糊需要保守处理？
- 对 SRR 中明确描述的场景，生成完整的 Given-When-Then 用例
- 对 SRR 中未明确但可推断的边界情况（如字段长度限制），生成时可标注较低置信度
- **严禁凭空发明 SRR 中不存在的新功能或新场景**——只基于 SRR 已有内容生成用例

### 2. 只做被要求的（Only what's asked）
- 每个用例必须能从 SRR 中找到对应依据（字段、规则、流程步骤）
- 如果某个测试维度（如安全测试）在 SRR 中没有足够信息，跳过该维度而不是编造
- gen_confidence 应诚实反映该用例与 SRR 的匹配程度

### 3. 清晰标注不确定（Surface ambiguity）
- 对于 SRR 信息不足但值得关注的测试点，生成用例但 gen_confidence 设为 0.3-0.5
- 在用例的 notes 字段中标注"信息不足，建议补充 SRR"

## 生成策略（按测试类型）

1. 功能测试 — Step-by-step 推导，覆盖 happy_path 和 alternate_flows
2. 边界值测试 — 等价类 + 边界分析，覆盖 data_constraints 中的边界
3. 异常测试 — 故障注入 + 防御推理，覆盖可预见的异常场景
4. 安全测试 — OWASP 检查清单（仅当 SRR 含安全需求时生成）
5. 性能测试 — 负载模型推断（仅当 SRR 含 non_functional.performance 时生成）
6. 组合测试 — Pairwise/正交表，参数交互组合（可选）

## 输出要求

1. 输出 UTCF JSON 格式（test_suite 数组）
2. 每个用例必须包含 related_requirement 字段（追溯）
3. 每个用例必须包含 gen_metadata.gen_confidence（0-1，诚实评估）
4. 用例 ID 格式：TC-{MODULE}-{4位序号}-{4位序号}
5. priority: P0=核心流程, P1=重要分支, P2=边界/异常, P3=探索性
6. 每个用例必须可独立执行，步骤含 action 和 expected
7. 测试数据应覆盖正常值、边界值、异常值
8. **不要编造 SRR 中不存在的内容**"""


# ============================================================================
# UTCF 用例 Schema 说明
# ============================================================================

UTCF_CASE_SCHEMA = """每个用例的 JSON 结构：

{
  "id": "TC-AUTH-0001-0001",
  "title": "用例标题（动词+对象+场景）",
  "type": "functional|boundary|exception|security|performance|integration|exploratory",
  "priority": "P0|P1|P2|P3",
  "description": "用例描述",
  "tags": [],
  "preconditions": ["前置条件"],
  "steps": [
    {"step": 1, "action": "操作描述", "expected": "预期结果", "data": {}}
  ],
  "expected": "整体预期结果（可选）",
  "test_data": {},
  "assertions": [
    {"type": "equals|contains|matches|status_code|gt|lt|not_null|custom",
     "target": "断言目标路径", "expected_value": null, "message": "断言说明"}
  ],
  "cleanup": ["清理操作"],
  "related_requirement": "REQ-YYYY-NNN",
  "source_pattern": "参考的测试模式 ID（可选）",
  "gen_metadata": {
    "gen_confidence": 0.8,
    "model": "",
    "prompt_version": "v1.0"
  },
  "execution": {
    "timeout_seconds": 15,
    "retry_count": 0,
    "depends_on": [],
    "skip_conditions": []
  }
}"""


# ============================================================================
# 用户指令构建函数
# ============================================================================

def build_user_prompt(
    srr: dict,
    few_shot_cases: list[dict] | None = None,
    matched_patterns: list[dict] | None = None,
    impact_guidance: str = "",
    enable_multi_type: bool = True,
) -> str:
    """构建 GenAgent 的用户指令。

    Args:
        srr: SRR JSON 字典
        few_shot_cases: 从知识库检索的相似历史用例（Top-K），用于 Few-shot 注入
        matched_patterns: 匹配到的测试模式列表，每项含 {name, pattern_json, category}
        impact_guidance: 影响分析指导文本（仅优化需求时传入，来自 ImpactAnalysisAgent）
        enable_multi_type: 是否启用分类生成（FULL 通道 True / FAST 通道 False）

    Returns:
        str: 完整的用户指令

    Note:
        - few_shot_cases 用于借鉴策略，但不要重复具体数据
        - impact_guidance 仅优化需求时传入，指导 update/add/delete/keep 操作
        - enable_multi_type=False 时单次批量生成，不按类型分类
    """
    few_shot_section = ""
    if few_shot_cases:
        few_shot_section = f"""

## 参考历史用例 (摘自知识库，仅借鉴策略，不要重复具体数据)
{json.dumps(few_shot_cases, ensure_ascii=False, indent=2)}"""

    patterns_section = ""
    if matched_patterns:
        patterns_section = f"""

## 测试模式参考
{json.dumps(matched_patterns, ensure_ascii=False, indent=2)}"""

    impact_section = ""
    if impact_guidance:
        impact_section = f"""

{impact_guidance}"""

    if enable_multi_type:
        gen_requirements = """## 生成要求
1. **功能测试** (≥ 3个) — 覆盖 happy_path 和 alternate_flows
2. **边界值测试** (≥ 2个) — 覆盖 data_constraints 中的边界
3. **异常测试** (≥ 2个) — 覆盖可预见的异常场景
4. **安全测试** (≥ 1个) — 覆盖安全关注点"""
    else:
        gen_requirements = """## 生成要求
- 生成核心测试用例（覆盖正常流程和主要异常场景），数量 3-5 个
- 不需要按类型分类，聚焦 happy_path 和关键 alternate_flows"""

    return f"""请根据以下需求，生成完整的测试用例集。

{UTCF_CASE_SCHEMA}
{few_shot_section}
{patterns_section}
{impact_section}

## 当前需求
{json.dumps(srr, ensure_ascii=False, indent=2)}

{gen_requirements}

## 输出格式
输出 JSON 数组，每个元素为一个测试用例（符合上述 UTCF_CASE_SCHEMA）。
直接输出 JSON，不要包含其他解释性文字。"""

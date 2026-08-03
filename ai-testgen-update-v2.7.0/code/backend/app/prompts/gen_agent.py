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
- 对 SRR 中未明确但可合理推断的边界/异常场景（如空输入、超长输入、并发），生成用例并标注 gen_confidence 0.3-0.5
- gen_confidence 应诚实反映该用例与 SRR 的匹配程度

### 3. 清晰标注不确定（Surface ambiguity）
- 对于 SRR 信息不足但值得关注的测试点，生成用例但 gen_confidence 设为 0.3-0.5
- 在用例的 notes 字段中标注"信息不足，建议补充 SRR"
- **宁可多生成低置信度用例，也不遗漏潜在风险场景**

## 生成策略（按测试类型）

1. 功能测试 — 覆盖 happy_path 每个步骤 + alternate_flows 每个分支，至少生成 5 个用例
2. 边界值测试 — 等价类+边界分析，对 data_constraints 中每个约束至少生成 1 个用例
3. 异常测试 — 故障注入+防御推理，对 alternate_flows 中每个异常至少生成 1 个用例
4. 安全测试 — OWASP 检查清单（始终生成至少 1 个安全检查用例）
5. 性能测试 — 负载模型推断（仅当 SRR 含 non_functional.performance 时生成）
6. 组合测试 — Pairwise/正交表，参数交互组合（SRR 含多参数交互时生成）

## 输出要求

1. 输出 JSON 数组（每个元素为一个测试用例，直接以 [ 开头，不要 test_suite 包装）
2. 每个用例必须包含 related_requirement 字段（追溯）
3. 每个用例必须包含 gen_metadata.gen_confidence（0-1，诚实评估）
4. 用例 ID 格式：TC-{MODULE}-{4位序号}-{4位序号}
5. priority: P0=核心流程, P1=重要分支, P2=边界/异常, P3=探索性
6. 每个用例必须可独立执行，步骤含 action 和 expected
7. 测试数据应覆盖正常值、边界值、异常值
8. **不要编造 SRR 中不存在的新功能**,但可以基于现有字段/规则合理推断边界、异常和组合场景
9. **宁可多生成低置信度用例,也不遗漏潜在风险**
10. **所有字段值必须是 JSON 字面量**（字符串、数字、布尔、null、数组、对象）
    - 严禁使用任何代码表达式、函数调用或非 JSON 语法
    - 错误示例：`"password": "a".repeat(128)`、`"username": "x" * 50`、`"data": Array(10).fill(0)`
    - 正确示例：`"password": "a*128"`、`"username": "x*50"`（用简短占位符表示长字符串）
    - 不要使用 `.repeat()` / `* N` / `Array()` 等代码构造
11. **字符串值长度限制**：每个字符串值不得超过 200 字符
    - 边界值测试中的超长输入（如 128 位密码），用简短描述表示，不要实际写出全部字符
    - 错误示例：`"password": "aaaaaa...（此处实际写出 128 个 a）"` — 会导致输出过长且 JSON 结构出错
    - 正确示例：`"password": "a重复128次"`、`"password": "aaa...(共128个a,省略中间部分)"`

## 输出示例（完整 UTCF 用例格式，请严格仿照此格式输出）

以「用户登录」SRR 为例，一个标准的功能测试用例如下：

```json
[
  {
    "id": "TC-AUTH-0001-0001",
    "title": "正确账号密码登录成功并跳转首页",
    "type": "functional",
    "priority": "P0",
    "description": "验证用户使用已注册的正确账号和密码可以成功登录系统并跳转到首页",
    "tags": ["登录", "核心流程"],
    "preconditions": ["账号已注册且未被锁定", "密码已设置"],
    "steps": [
      {"step": 1, "action": "打开登录页面", "expected": "显示登录表单（账号输入框、密码输入框、登录按钮）", "data": {}},
      {"step": 2, "action": "输入已注册账号 user@example.com 和正确密码 pass123456", "expected": "输入框正常显示输入内容", "data": {"account": "user@example.com", "password": "pass123456"}},
      {"step": 3, "action": "点击「登录」按钮", "expected": "系统校验通过，跳转至首页，页面显示用户昵称", "data": {}}
    ],
    "expected": "登录成功，跳转首页并显示用户昵称",
    "test_data": {
      "valid_account": {"account": "user@example.com", "password": "pass123456"}
    },
    "assertions": [
      {"type": "status_code", "target": "http.response.code", "expected_value": 200, "message": "登录接口返回200"},
      {"type": "contains", "target": "page.content", "expected_value": "用户昵称", "message": "首页显示用户昵称"},
      {"type": "not_null", "target": "session.token", "expected_value": null, "message": "会话令牌不为空"}
    ],
    "cleanup": ["退出登录", "清除浏览器缓存"],
    "related_requirement": "REQ-LOGIN-001",
    "related_fp": "FP-用户登录验证",
    "source_pattern": "",
    "gen_metadata": {
      "gen_confidence": 0.95,
      "model": "deepseek-v3",
      "prompt_version": "v2.0"
    },
    "execution": {
      "timeout_seconds": 15,
      "retry_count": 0,
      "depends_on": [],
      "skip_conditions": []
    }
  },
  {
    "id": "TC-AUTH-0001-0002",
    "title": "密码错误提示并累计失败次数不锁定",
    "type": "exception",
    "priority": "P1",
    "description": "验证输入错误密码时系统正确提示且失败计数递增，但未达锁定阈值",
    "tags": ["登录", "异常流程"],
    "preconditions": ["账号已注册且未被锁定", "当前失败次数 < 4"],
    "steps": [
      {"step": 1, "action": "打开登录页面", "expected": "显示登录表单", "data": {}},
      {"step": 2, "action": "输入账号 user@example.com 和错误密码 wrong123", "expected": "密码脱敏显示", "data": {"account": "user@example.com", "password": "wrong123"}},
      {"step": 3, "action": "点击「登录」按钮", "expected": "提示「账号或密码错误」，失败次数+1，不锁定账号", "data": {}}
    ],
    "expected": "提示密码错误，失败计数递增，不锁定",
    "test_data": {"wrong_password": {"account": "user@example.com", "password": "wrong123"}},
    "assertions": [
      {"type": "equals", "target": "response.message", "expected_value": "账号或密码错误", "message": "错误提示正确"},
      {"type": "equals", "target": "account.locked", "expected_value": false, "message": "账号未被锁定"}
    ],
    "cleanup": [],
    "related_requirement": "REQ-LOGIN-001",
    "related_fp": "FP-用户登录验证",
    "source_pattern": "",
    "gen_metadata": {
      "gen_confidence": 0.90,
      "model": "deepseek-v3",
      "prompt_version": "v2.0"
    },
    "execution": {
      "timeout_seconds": 15,
      "retry_count": 0,
      "depends_on": [],
      "skip_conditions": []
    }
  }
]
```

关键要点：
- 每个用例必须有完整的 steps（含 action + expected）
- assertions 明确断言目标和期望值
- gen_metadata.gen_confidence 诚实反映与 SRR 的匹配度
- related_fp 标注对应的功能点ID
- 测试数据覆盖正常值/边界值/异常值"""


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
# SRR 覆盖率检查清单构建（v3.3：增强 SRR 对用例生成的指导）
# ============================================================================

def _build_coverage_checklist(srr: dict, functional_points: list[dict]) -> str:
    """从 SRR 中显式提取所有必须覆盖的要点，构建覆盖率检查清单。

    让 LLM 清楚知道需要覆盖多少个点，提高覆盖率和生成稳定性。

    Args:
        srr: SRR JSON 字典
        functional_points: 功能点列表

    Returns:
        str: 覆盖率检查清单文本（注入 prompt）
    """
    items: list[str] = []

    # 1. 总 SRR 的 happy_path
    flows = srr.get("flows", {}) if isinstance(srr, dict) else {}
    happy_path = flows.get("happy_path", {})
    hp_steps = happy_path.get("steps", []) if isinstance(happy_path, dict) else []
    if hp_steps:
        items.append(f"### 总 SRR 主流程（happy_path）：{len(hp_steps)} 个步骤")
        for i, step in enumerate(hp_steps, 1):
            if isinstance(step, dict):
                action = step.get("action", step.get("desc", ""))
                items.append(f"  - [happy_path.{i}] {action}")
            else:
                items.append(f"  - [happy_path.{i}] {step}")

    # 2. 总 SRR 的 alternate_flows
    alt_flows = flows.get("alternate_flows", [])
    if alt_flows:
        items.append(f"### 总 SRR 异常分支（alternate_flows）：{len(alt_flows)} 个分支")
        for i, af in enumerate(alt_flows, 1):
            if isinstance(af, dict):
                name = af.get("name", af.get("trigger", ""))
                items.append(f"  - [alternate_flow.{i}] {name}")
            else:
                items.append(f"  - [alternate_flow.{i}] {af}")

    # 3. 总 SRR 的 preconditions
    preconds = srr.get("preconditions", [])
    if preconds:
        items.append(f"### 总 SRR 前置条件（preconditions）：{len(preconds)} 条")
        for i, p in enumerate(preconds, 1):
            items.append(f"  - [precondition.{i}] {p}")

    # 4. 总 SRR 的跨 FP 交互规则
    cfi = srr.get("cross_fp_interactions", {}) if isinstance(srr, dict) else {}
    shared_rules = cfi.get("shared_business_rules", [])
    if shared_rules:
        items.append(f"### 跨 FP 共享业务规则：{len(shared_rules)} 条")
        for i, r in enumerate(shared_rules, 1):
            if isinstance(r, dict):
                desc = r.get("rule", r.get("description", ""))
                items.append(f"  - [shared_rule.{i}] {desc}")
            else:
                items.append(f"  - [shared_rule.{i}] {r}")

    # 5. 各功能点的覆盖要点
    for fp in functional_points:
        fp_id = fp.get("id", "")
        fp_name = fp.get("name", fp_id)
        fp_srr = fp.get("srr_json", {}) if isinstance(fp.get("srr_json"), dict) else {}

        items.append(f"### 功能点 [{fp_id}] {fp_name}")

        # FP happy_path
        fp_flows = fp_srr.get("flows", {})
        fp_hp = fp_flows.get("happy_path", {})
        fp_hp_steps = fp_hp.get("steps", []) if isinstance(fp_hp, dict) else []
        if fp_hp_steps:
            items.append(f"  主流程步骤：{len(fp_hp_steps)} 个")
            for i, step in enumerate(fp_hp_steps, 1):
                if isinstance(step, dict):
                    action = step.get("action", step.get("desc", ""))
                    items.append(f"  - [{fp_id}.happy_path.{i}] {action}")
                else:
                    items.append(f"  - [{fp_id}.happy_path.{i}] {step}")

        # FP alternate_flows
        fp_alt = fp_flows.get("alternate_flows", [])
        if fp_alt:
            items.append(f"  异常分支：{len(fp_alt)} 个")
            for i, af in enumerate(fp_alt, 1):
                if isinstance(af, dict):
                    name = af.get("name", af.get("trigger", ""))
                    items.append(f"  - [{fp_id}.alt_flow.{i}] {name}")
                else:
                    items.append(f"  - [{fp_id}.alt_flow.{i}] {af}")

        # FP business_rules
        fp_rules = fp_srr.get("business_rules", [])
        if fp_rules:
            items.append(f"  业务规则：{len(fp_rules)} 条")
            for i, r in enumerate(fp_rules, 1):
                if isinstance(r, dict):
                    desc = r.get("rule", r.get("description", r.get("condition", "")))
                    items.append(f"  - [{fp_id}.rule.{i}] {desc}")
                else:
                    items.append(f"  - [{fp_id}.rule.{i}] {r}")

        # FP data_constraints
        fp_dc = fp_srr.get("data_constraints", [])
        if fp_dc:
            items.append(f"  数据约束：{len(fp_dc)} 条")
            for i, d in enumerate(fp_dc, 1):
                if isinstance(d, dict):
                    field = d.get("field", "")
                    cons = d.get("constraints", d.get("constraint", ""))
                    items.append(f"  - [{fp_id}.data.{i}] {field}: {cons}")
                else:
                    items.append(f"  - [{fp_id}.data.{i}] {d}")

        # v2.48: boundary_conditions 已合并到 business_rules / alternate_flows，不再单独列出
        # v2.48: acceptance_criteria 已合并到 business_rules（type=validation），不再单独列出
        # v2.48: test_hints 已移除，测试提示由用例生成阶段自行推导

    if not items:
        return ""

    return "## SRR 覆盖率检查清单（必须覆盖以下所有要点）\n\n" + "\n".join(items)


# ============================================================================
# 用户指令构建函数
# ============================================================================

def build_user_prompt(
    srr: dict,
    few_shot_cases: list[dict] | None = None,
    matched_patterns: list[dict] | None = None,
    impact_guidance: str = "",
    enable_multi_type: bool = True,
    functional_points: list[dict] | None = None,
) -> str:
    """构建 GenAgent 的用户指令。

    Args:
        srr: SRR JSON 字典
        few_shot_cases: 从知识库检索的相似历史用例（Top-K），用于 Few-shot 注入
        matched_patterns: 匹配到的测试模式列表，每项含 {name, pattern_json, category}
        impact_guidance: 影响分析指导文本（仅优化需求时传入，来自 ImpactAnalysisAgent）
        enable_multi_type: 是否启用分类生成（FULL 通道 True / FAST 通道 False）
        functional_points: 需求已拆分的功能点列表（可选）。每项含
            {id, name, fp_type, test_hints, atomicity_check, summary}。
            传入时 LLM 将按 FP 定向生成用例，并在每个用例的 related_fp 字段
            标注对应的功能点 ID，用于后续 test_case↔functional_point 追溯。

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

    # 功能点定向生成上下文（v1.4：FP 接入生成流水线 / v1.5.3：消费 SRR 统一格式）
    fp_section = ""
    fp_requirements = ""
    if functional_points:
        # v2.34: 注入总 SRR 的跨 FP 共享规则，供 LLM 生成跨 FP 集成用例参考
        cfi = srr.get("cross_fp_interactions") or {} if isinstance(srr, dict) else {}
        shared_rules = cfi.get("shared_business_rules", []) or []
        fp_brief = [
            {
                "id": fp.get("id", ""),
                "name": fp.get("name", ""),
                "fp_type": fp.get("fp_type", "workflow"),
                "summary": fp.get("srr_json", {}).get("requirement", {}).get("summary", fp.get("srr_json", {}).get("summary", "")),
                "preconditions": fp.get("srr_json", {}).get("preconditions", []),
                "happy_path_steps": fp.get("srr_json", {}).get("flows", {}).get("happy_path", {}).get("steps", []) or
                                    ([{"order": 1, "action": fp.get("srr_json", {}).get("flows", {}).get("happy_path", ""),
                                       "expect": ""}] if isinstance(fp.get("srr_json", {}).get("flows", {}).get("happy_path", ""), str) else []),
                "alternate_flows": fp.get("srr_json", {}).get("flows", {}).get("alternate_flows", []),
                "business_rules": fp.get("srr_json", {}).get("business_rules", []),
                "data_constraints": fp.get("srr_json", {}).get("data_constraints", []),
                "non_functional": fp.get("srr_json", {}).get("non_functional", {}),
                "atomicity_check": fp.get("atomicity_check", {}),
            }
            for fp in functional_points
        ]
        fp_section = f"""

## 功能点拆分（已拆分的原子功能单元，按此定向生成用例）
{json.dumps(fp_brief, ensure_ascii=False, indent=2)}"""
        # v2.34: 注入跨 FP 共享规则，供 LLM 生成跨 FP 集成/交互用例参考
        if shared_rules:
            fp_section += f"""

## 跨 FP 共享业务规则（总 SRR 的 cross_fp_interactions.shared_business_rules，适用于多个 FP）
{json.dumps(shared_rules, ensure_ascii=False, indent=2)}

**覆盖要求**：若某条共享规则 applied_fps 涉及 ≥2 个 FP，应生成一个跨 FP 集成用例验证该规则的端到端一致性。"""
        fp_requirements = """
### 功能点覆盖要求（优先级最高）
- 每个功能点（FP）至少生成 1 个测试用例，覆盖其 happy_path_steps 中的每个步骤
- 若 alternate_flows 非空：每个异常分支生成 1 个用例（从 name/trigger/expect 推导）
- 若 data_constraints 中有 required=true 的字段：补充必填校验用例
- 若 business_rules 中有 type=security/validation/workflow 的规则：生成对应类型的用例
- v2.48: business_rules 已包含原验收标准（type=validation/business），验收用例从 business_rules 推导
- 在每个用例中添加 "related_fp" 字段，值为此用例对应的功能点 ID
- preconditions 作为用例前置条件"""

    # v3.0：注入 SRR 中的影响功能说明（来自依赖图谱 FP 节点点击生成）
    impact_analysis_section = ""
    srr_impact = srr.get("impact_analysis", []) if isinstance(srr, dict) else []
    if srr_impact:
        impact_brief = [
            {
                "fp_id": item.get("fp_id", ""),
                "fp_name": item.get("fp_name", ""),
                "affected_fps": [
                    {"fp_name": f.get("fp_name", ""), "relation": f.get("relation_cn", f.get("relation_type", "")), "req_title": f.get("req_title", "")}
                    for f in item.get("affected_fps", [])[:10]
                ],
                "affected_modules": [
                    {"module_name": m.get("module_name", ""), "reason": m.get("reason", "")}
                    for m in item.get("affected_modules", [])[:10]
                ],
                # v2.35: 移除 affected_requirements（不再分析受影响的需求）
                "description": item.get("description", ""),
                "test_focus": item.get("test_focus", ""),
            }
            for item in srr_impact
        ]
        impact_analysis_section = f"""

## 影响功能说明（来自依赖图谱分析，指导回归用例生成）
{json.dumps(impact_brief, ensure_ascii=False, indent=2)}

### 影响分析覆盖要求
- 针对 description 中提到的受影响对象，生成回归测试用例
- test_focus 列出的场景必须全部覆盖
- 受影响 FP 的 happy_path 必须生成回归用例
- 受影响模块下的关键需求，至少生成 1 个跨模块影响验证用例
- 在回归用例的 related_fp 字段中标注受影响的 fp_id"""

    # v3.3：SRR 覆盖率检查清单——从 SRR 中显式提取所有必须覆盖的要点
    # 让 LLM 清楚知道需要覆盖多少个点，提高覆盖率和稳定性
    coverage_checklist = _build_coverage_checklist(srr, functional_points or [])

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
{fp_section}
{impact_analysis_section}

{coverage_checklist}

## 当前需求
{json.dumps(srr, ensure_ascii=False, indent=2)}

{gen_requirements}
{fp_requirements}

## 覆盖率自检（生成前必读）
在生成用例之前，请先对照上面的「SRR 覆盖率检查清单」：
1. 确认每个 happy_path 步骤都有对应的用例覆盖
2. 确认每个 alternate_flow 分支都有对应的异常/功能用例
3. 确认每条 business_rule 都有验证用例（含原验收标准，type=validation/business）
4. 确认每条 data_constraint 都有边界值用例
5. 生成完成后，在 notes 字段中标注该用例覆盖了检查清单中的哪些条目（如 "覆盖: happy_path.step1, happy_path.step2"）

## 输出格式
输出 JSON 数组，每个元素为一个测试用例（符合上述 UTCF_CASE_SCHEMA）。
直接输出 JSON，不要包含其他解释性文字。"""

# -*- coding: utf-8 -*-
"""ReqAgent需求解析提示词。

将原始需求文本(PRD/JIRA/用户故事/纯文本)解析为结构化SRR JSON。
采用CoT(Chain-of-Thought)分步提取策略，确保字段完整。

设计依据: docs/01-requirement-parsing.md 3.1(ReqAgent核心Prompt)
解析流程(docs/01 3):
    1. 格式自动检测 -> 2. 预处理 -> 3. LLM结构化解析 ->
    4. 字段校验 -> 5. 实体链接 -> 6. 测试点提示生成 -> SRR JSON
"""

from __future__ import annotations

# ============================================================================
# 系统指令
# ============================================================================

SYSTEM_PROMPT = """你是一个专业的软件需求分析师。请将以下需求文本解析为结构化的 JSON 格式。

## 核心原则

### 1. 完整性优先（Don't lose information）
你最重要的任务是**确保原始需求文本中的每一个信息点都被提取并映射到输出 JSON 中**。
不得遗漏任何内容。如果原始文本提供了详细信息（如步骤、规则、字段定义），
输出中必须包含同样详细的信息。

### 2. 禁止编造（Don't fabricate）
- **只提取文档中明确写出的内容**——不要假设或补充文档未提及的功能、规则、流程
- 如果某个字段（如安全需求、性能指标）原文未提及，对应字段留空或设为 null
- 不要让"专业经验"补充文档中没有的内容

### 3. 区分确定性（Mark confidence）
- 原文明确描述 → 直接提取
- 原文含糊（如"支持多种登录方式"）→ 提取原文，不展开猜测
- 不确定是否该提取 → 宁可遗漏不提取，也不编造

## 增量文档识别与处理
如果需求文档自我声明是某个版本的增量 / 优化项，则进入**增量模式**：
- 识别信号：标题含版本号（如 V2.0 / 第二版），或正文含「相对于 V1」「新增与优化」「完整上下文请参见」「本文档仅描述」「优化版本」等表述。
- 增量模式下：
  1. **正常提取文档显式描述的所有内容**
  2. **严禁编造文档未提及的基础 / 通用内容**：不要假设密码规则、不要补全登录主流程的通用步骤、不要编造错误提示文案、不要虚构文档未说明的约束
  3. 顶层输出 `"_is_incremental": true`
  4. 输出 `"_baseline_gaps"` 字符串数组，列出真正缺失且无法推断的基础维度
- 如果文档是完整独立需求，不输出 `_is_incremental`，按正常完整性优先原则完整提取。

## 解析策略（按顺序执行）
1. **完整性扫描** - 通读整篇需求，先列出所有信息点，再逐个映射
2. **识别功能概述** - 用一句话说清楚这个需求做什么
3. **提取前置条件** - 完成此功能需要满足什么
4. **描述正常流程** - 按步骤列出 happy path（每步含 action + expect）
5. **枚举异常场景** - 思考所有可能的错误/异常情况
6. **识别边界条件** - 输入/输出的临界值
7. **提取业务规则** - 计算公式、约束条件
8. **整理数据约束** - 字段、类型、校验规则
9. **标注非功能需求** - 性能、安全、兼容性

## 完整性检查
输出完成后，请逐条对照原始需求原文，确保:
- 每个功能点都已映射到 SRR 字段
- 每个步骤/规则/条件的细节都已保留(不要简化、不要概括)
- 原文中的具体数值、名称、条件都已准确提取
- 如果原文包含多段多层次的描述，每段都要覆盖到
- summary 字段必须全面概括需求内容(不少于 50 字)

注意:
- 需求原文中明确提到的内容，**必须**完整保留在输出中
- 异常场景需要包含"触发条件"和"预期结果"
- test_points_hint 是推断的测试关注点，不是最终测试用例
- priority 必须是 P0/P1/P2/P3 之一，默认 P2
- modules 字段为数组，支持多模块关联
- 如果原文内容非常丰富，**不要省略任何细节**，输出 JSON 可以很长
- `additional_details` 字段用于存放无法映射到标准字段的原文内容
"""


# ============================================================================
# SRR Schema 说明(注入用户指令，约束 LLM 输出结构)
# ============================================================================

SRR_SCHEMA_GUIDE = """输出 JSON 必须包含以下字段(对应 SRR Schema):

{
  "srr_version": "1.0",
  "requirement": {
    "id": "REQ-YYYY-NNN",          // 需求 ID，原文无则留空
    "product_id": "PROD-XXX",       // 产品 ID，原文无则用 "PROD-DEFAULT"
    "product_name": "",             // 产品名称
    "title": "",                    // 需求标题(必填)
    "priority": "P0|P1|P2|P3",      // 优先级，默认 P2
    "modules": [],                  // 关联模块列表
    "summary": ""                   // 需求概述，需**全面概括**整篇需求(不少于50字)
  },
  "preconditions": [],              // 前置条件列表(字符串数组)
  "flows": {
    "happy_path": {
      "steps": [                    // 正常流程步骤(必填，步骤越多越好)
        {"order": 1, "action": "", "expect": ""}
      ]
    },
    "alternate_flows": [            // 异常/分支流程
      {"name": "", "trigger": "", "expect": ""}
    ]
  },
  "boundary_conditions": [          // 边界条件
    {"field": "", "condition": "", "expect": ""}
  ],
  "business_rules": [               // 业务规则
    {"id": "BR-01", "rule": "", "type": "validation|calculation|workflow|permission"}
  ],
  "data_constraints": [             // 数据约束(必填)
    {"field": "", "type": "string|integer|date|boolean", "required": false,
     "constraints": "", "example": ""}
  ],
  "non_functional": {               // 非功能需求
    "performance": "",
    "security": "",
    "compatibility": "",
    "accessibility": ""
  },
  "relationships": [                // 关联需求
    {"target": "REQ-xxx", "type": "depends_on|related_to|conflicts|replaces",
     "strength": "strong|weak", "note": ""}
  ],
  "test_points_hint": [             // 测试点提示(推断，非最终用例)
    {"category": "functional|boundary|exception|security|performance", "point": ""}
  ],

  // ===== v2.1 新增字段(v2.1: 完整映射需求原文) =====
  "acceptance_criteria": [          // 验收标准/验收条件
    {"id": "AC-01", "description": "", "type": "functional|performance|security|usability"}
  ],
  "api_definitions": [              // API接口定义
    {"method": "GET|POST|PUT|PATCH|DELETE", "path": "/api/xxx",
     "summary": "", "request_body": "", "response_body": "",
     "error_responses": []}
  ],
  "user_roles": [                   // 用户角色/权限
    {"role": "", "description": "", "permissions": []}
  ],
  "dependencies": [                 // 外部依赖
    {"name": "", "type": "API|DB|第三方服务",
     "description": "", "impact_if_unavailable": ""}
  ],
  "ui_rules": [                     // 界面/交互规则
    {"element": "", "behavior": "", "condition": ""}
  ],
  "data_model": [                   // 数据模型/实体关系
    {"entity_name": "", "description": "", "fields": [], "relationships": []}
  ],
  "assumptions": [                  // 假设与约束
    {"content": "", "type": "assumption|constraint|out_of_scope"}
  ],
  "additional_details": ""          // 原文中无法映射到上述标准字段的额外内容
                                    //   (如设计备注、集成说明等)，原文照搬

  // ===== 增量文档标记（v2.9: 文档自我声明为某版本增量/优化项时输出） =====
  "_is_incremental": false,         // 增量标记：true 表示本需求为增量文档，依赖历史版本（如 V1）的基础上下文
  "_baseline_gaps": [],             // 增量文档声明需从知识库（同产品历史需求）补全的基础维度列表

  // ===== 影响功能说明（v3.0: 由依赖图谱 FP 节点点击触发生成，可编辑） =====
  "impact_analysis": []             // 影响分析数组，每项含 {fp_id, fp_name, affected_fps, affected_modules,
                                    //   affected_requirements, description, test_focus, generated_at, is_auto_generated}
                                    //   默认空数组，由后端端点 POST /requirements/{id}/fp/{fp_id}/impact-description 生成
}"""


# ============================================================================
# 用户指令构建函数
# ============================================================================

def build_user_prompt(raw_requirement: str, product_id: str = "", product_name: str = "") -> str:
    """构建 ReqAgent 的用户指令。

    Args:
        raw_requirement: 原始需求文本(Markdown/JSON/JIRA/纯文本)
        product_id: 产品 ID(可选，由调用方传入则注入到 prompt)
        product_name: 产品名称(可选)

    Returns:
        str: 完整的用户指令

    Note:
        注入 SRR Schema 说明，约束 LLM 输出符合 SRR 结构。
    """
    product_hint = ""
    if product_id or product_name:
        product_hint = f"\n\n【产品信息】\nproduct_id: {product_id}\nproduct_name: {product_name}\n"

    return f"""请解析以下需求文本为 SRR JSON 格式。

{SRR_SCHEMA_GUIDE}
{product_hint}
【需求文本】
{raw_requirement}

请直接输出 SRR JSON，不要包含其他解释性文字。"""


# ============================================================================
# Few-shot 示例(注入到系统指令，提升解析一致性)
# ============================================================================

FEW_SHOT_EXAMPLE = """【示例1: 中等复杂度需求】
输入需求:
"用户登录功能: 用户通过账号密码登录系统，连续失败5次锁定30分钟，登录成功后跳转首页。需支持记住密码功能，密码使用bcrypt加密存储。"

输出 SRR JSON:
{
  "srr_version": "1.0",
  "requirement": {
    "id": "",
    "product_id": "PROD-DEFAULT",
    "product_name": "",
    "title": "用户登录功能",
    "priority": "P0",
    "modules": ["auth", "session"],
    "summary": "用户通过账号密码登录系统，支持记住密码功能。连续登录失败5次后账号锁定30分钟。密码使用bcrypt加密存储，登录成功后跳转至首页。"
  },
  "preconditions": ["用户已在系统注册", "用户账号未处于锁定状态"],
  "flows": {
    "happy_path": {
      "steps": [
        {"order": 1, "action": "打开登录页", "expect": "显示登录表单，包含账号输入框、密码输入框、记住密码复选框、登录按钮"},
        {"order": 2, "action": "输入已注册的账号和正确的密码", "expect": "输入框正常显示输入内容"},
        {"order": 3, "action": "勾选「记住密码」(可选)", "expect": "下次登录自动填充账号密码"},
        {"order": 4, "action": "点击登录按钮", "expect": "系统校验账号密码，登录成功，跳转至首页"}
      ]
    },
    "alternate_flows": [
      {"name": "账号不存在", "trigger": "输入未注册的账号", "expect": "提示「账号不存在」"},
      {"name": "密码错误", "trigger": "输入错误密码", "expect": "提示「密码错误」，记录失败次数"},
      {"name": "连续5次密码错误", "trigger": "同一账号连续5次输入错误密码", "expect": "触发锁定机制，提示「账号已锁定，30分钟后重试」"},
      {"name": "已锁定账号尝试登录", "trigger": "锁定期间再次尝试登录", "expect": "提示「账号已被锁定，请30分钟后重试」"}
    ]
  },
  "boundary_conditions": [
    {"field": "密码", "condition": "长度 < 6", "expect": "前端校验拦截，提示密码长度不足"},
    {"field": "密码", "condition": "长度 = 6", "expect": "允许提交，后端进一步校验"},
    {"field": "密码", "condition": "长度 > 128", "expect": "前端校验拦截，提示密码过长"},
    {"field": "锁定次数", "condition": "连续失败4次", "expect": "显示剩余尝试次数1次"},
    {"field": "锁定次数", "condition": "连续失败5次", "expect": "触发锁定，计时30分钟"}
  ],
  "business_rules": [
    {"id": "BR-01", "rule": "连续失败5次锁定30分钟", "type": "security"},
    {"id": "BR-02", "rule": "密码使用bcrypt加密存储", "type": "security"}
  ],
  "data_constraints": [
    {"field": "username", "type": "string", "required": true, "constraints": "3-50个字符", "example": "john_doe"},
    {"field": "password", "type": "string", "required": true, "constraints": "6-128个字符", "example": "***"}
  ],
  "non_functional": {
    "performance": "登录响应P99 < 500ms",
    "security": "TLS 1.3传输加密，密码bcrypt加密存储，防暴力破解(5次锁定)",
    "compatibility": "支持主流浏览器Chrome/Firefox/Safari/Edge最新版本",
    "accessibility": ""
  },
  "relationships": [],
  "test_points_hint": [
    {"category": "functional", "point": "正常登录流程验证"},
    {"category": "functional", "point": "记住密码功能验证"},
    {"category": "boundary", "point": "密码长度边界值(6-128字符)"},
    {"category": "boundary", "point": "锁定次数边界(4次/5次)"},
    {"category": "exception", "point": "账号锁定机制(锁定后30分钟内不可登录)"},
    {"category": "security", "point": "密码加密存储验证"}
  ],
  "additional_details": "需支持记住密码功能，密码使用bcrypt加密存储"
}"""

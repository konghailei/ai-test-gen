"""
功能点（FunctionalPoint）拆分与关联分析提示词（v1.4）。

设计依据：
- BP→FP→Data 三级粒度模型（51testing）
- FP 判定规则 R1-R4（含优先级）
- FP 类型体系 6 种（calculation/constraint/workflow/api_behavior/data_record/config）
- 三要素原子性标准
"""
from __future__ import annotations

# ============================================================================
# v1.5.1: 一体式 FP 拆分 — 输入原始文档 + 概要 SRR → 输出 FP 列表 + 子 SRR
# 替代旧的三步流（提取 BP → 勾选 → 逐 BP 拆分），解决 6000 字符截断和固定输出问题
# ============================================================================
FP_INTEGRATED_SYSTEM_PROMPT = """你是一个功能点（FP）拆分专家 + SRR 架构师。
输入是一份原始需求文档和其对应的概要 SRR。

你的任务有两个：
1. 从原始文档中识别所有**独立的业务能力**作为功能点，每个 FP 附带一份完整的子 SRR（内部规则）
2. 同时输出一份**交互层 SRR**（interaction_srr），只包含 FP 之间的交互规则、共享约束、数据契约

## 交互层 vs 内部层的划分规则（消除重复）

总 SRR（interaction_srr）**只保留**：
  • cross_fp_flows：FP 之间的调用顺序、数据传递契约
    例：「登录验证成功后，需传递 session_token + user_id 给首页跳转」
  • shared_business_rules：同时适用于 ≥2 个 FP 的规则
    例：「全链路 TLS 1.3 加密」影响登录、注册、数据传输等多个 FP
  • fp_contracts：FP 之间的接口约定
    例：「密码校验结果以布尔值回调，不落库」
  • global_constraints：全局性约束（不属于任何单一 FP 的业务规则）
  • acceptance_criteria：仅跨 FP 的验收标准

每个 FP 的内部 sub_srr **保留**：
  • 该 FP 独有的流程步骤（happy_path / alternate_flows）
  • 该 FP 独有的业务规则、数据约束、验收标准
  • 属于单个 FP 的校验逻辑、异常处理、状态流转

**核心原则**：
  • 一条规则只出现一次 — 单 FP 专属 → 放 sub_srr；跨 FP 或多 FP 共享 → 放 interaction_srr
  • 宁缺毋滥：不确定该放哪的规则优先放 FP 的 sub_srr；只有明确跨 FP 的才放 interaction_srr
  • 禁止同一规则在 interaction_srr 和 sub_srr 中重复出现

## 核心原则：FP = 可独立交付的业务能力

FP 的粒度是**业务能力级**，不是操作步骤级。一个 FP 是一个有业务价值的、可以独立描述和验证的功能模块。

## 粒度判定规则

规则 1（最重要）：**一个业务概念 = 一个 FP**。不要把同一业务功能的正常流程、异常处理、校验规则拆成多个 FP。
  ✅ 正确：「用户登录」→ 1 个 FP（包含：账号密码输入、密码校验、登录成功跳转、登录失败提示、连续失败锁定、会话超时、记住密码——全部在 sub_srr 内部描述）
  ❌ 错误：「输入账号密码」「密码格式校验」「登录成功跳转」「登录失败提示」「连续失败锁定」各一个 FP

规则 2：**不同业务实体/能力 = 不同 FP**。两个不相关的业务功能才需要独立拆分。
  ✅ 正确：「用户注册」「用户登录」→ 2 个 FP（它们是不同的业务能力）
  ✅ 正确：「用户管理」「订单管理」「商品管理」→ 3 个 FP

规则 3：**表单/字段校验不独立**。校验规则属于所属 FP 的 business_rules 和 data_constraints，不单独抽为一个 FP。
  ✅ 正确：密码长度≥6 位、用户名不能为空 → 写在「用户注册」FP 的 business_rules 里
  ❌ 错误：把「密码格式校验」单独拆为一个 constraint 类型的 FP

规则 4：**异常分支不独立**。同一功能的异常场景（超时、权限不足、输入非法）属于该 FP 的 alternate_flows，不另立 FP。
  ✅ 正确：「订单支付」的 sub_srr.alternate_flows = "余额不足→提示充值；超时→取消订单"

## 子 SRR 格式（与主 SRR 统一，下游 gen_agent 共享解析逻辑）

每个 FP 的 sub_srr 复用主 SRR 的结构化字段，确保：
- flows.happy_path 使用结构化步骤（order/action/expect），不是纯文本
- alternate_flows 用对象数组（name/trigger/expect）
- business_rules 用对象数组（id/rule/type）
- data_constraints 用完整对象（field/type/required/constraints/example）
- acceptance_criteria 用对象数组（id/description/type），**type 只能从以下值中选择**：functional/security/performance/usability/compatibility/reliability

## 生成约束（禁止编造）

所有输出内容必须严格基于原始需求文档中的描述。**禁止**：
- 添加原始需求文档中未提及的功能、流程步骤、业务规则或字段
- 假设或推断不存在的异常场景、边界条件或验收标准
- 补充需求文档中没有的技术实现细节、接口定义或数据约束
- 伪造业务规则（business_rules）或验收标准（acceptance_criteria）的具体内容

如果原始需求文档对某个字段的信息不足，该字段留空或使用默认值，不要编造内容填充。

## 输出格式（JSON）
{
  "interaction_srr": {
    "requirement": {
      "title": "用户登录优化 V2",
      "summary": "在 V1 登录功能基础上增加图形验证码机制",
      "priority": "P1"
    },
    "cross_fp_flows": [
      {"from": "登录验证", "to": "首页跳转", "contract": "登录成功后传递 session_token + user_id"},
      {"from": "图形验证码", "to": "登录验证", "contract": "验证码通过后才允许提交登录请求"}
    ],
    "shared_business_rules": [
      {"id": "BR-SHR-001", "rule": "全链路 TLS 1.3 加密", "type": "security", "applies_to": ["登录验证", "数据传输"]}
    ],
    "fp_contracts": [
      {"fp_a": "登录验证", "fp_b": "密码复杂度", "contract": "密码校验结果以布尔值回调，不落库"}
    ],
    "global_constraints": [
      {"id": "DC-GLOBAL-001", "field": "所有接口", "constraints": "请求频率不超过 100/min"}
    ],
    "acceptance_criteria": [
      {"id": "AC-INT-001", "description": "FP 之间数据传递无误且无信息丢失", "type": "functional"}
    ]
  },
  "functional_points": [
    {
      "name": "用户登录",
      "fp_type": "workflow",
      "priority": "P1",
      "summary": "用户通过账号密码完成身份认证，进入系统",
      "suggested_module": "用户中心",
      "sub_srr": {
        "requirement": {
          "title": "用户登录",
          "summary": "用户输入账号密码后系统校验身份，成功跳转首页，失败提示错误，连续5次失败锁定30分钟",
          "priority": "P1"
        },
        "preconditions": ["用户已注册", "账号未被禁用", "账号未被锁定"],
        "flows": {
          "happy_path": {
            "steps": [
              {"order": 1, "action": "用户输入账号和密码", "expect": "输入框显示输入内容"},
              {"order": 2, "action": "用户点击「登录」按钮", "expect": "系统开始校验身份"},
              {"order": 3, "action": "系统校验账号密码", "expect": "校验通过，生成 session"},
              {"order": 4, "action": "跳转首页", "expect": "页面跳转到系统首页"}
            ]
          },
          "alternate_flows": [
            {"name": "账号不存在", "trigger": "账号在系统中未找到", "expect": "提示「账号不存在」"},
            {"name": "密码错误", "trigger": "密码不匹配", "expect": "提示「密码错误」并累计失败次数"},
            {"name": "账号锁定", "trigger": "连续5次密码错误", "expect": "提示「账号已锁定，请30分钟后重试」"},
            {"name": "会话超时", "trigger": "2小时无操作", "expect": "跳转登录页"}
          ]
        },
        "boundary_conditions": [
          {"field": "账号", "condition": "空值", "expect": "提示「请输入账号」"},
          {"field": "密码", "condition": "空值", "expect": "提示「请输入密码」"},
          {"field": "密码", "condition": "少于6位", "expect": "提示「密码至少6位」"}
        ],
        "business_rules": [
          {"id": "BR-LOGIN-001", "rule": "连续5次登录失败锁定账号30分钟", "type": "security"},
          {"id": "BR-LOGIN-002", "rule": "密码长度≥6位", "type": "validation"},
          {"id": "BR-LOGIN-003", "rule": "登录成功后重置失败计数", "type": "workflow"},
          {"id": "BR-LOGIN-004", "rule": "会话有效期2小时，超时自动退出", "type": "session"},
          {"id": "BR-LOGIN-005", "rule": "密码输入框默认脱敏显示", "type": "presentation"},
          {"id": "BR-LOGIN-006", "rule": "页面加载时清除上次未完成的登录状态", "type": "initialization"},

        ],
        "data_constraints": [
          {"field": "账号", "type": "string", "required": true, "constraints": "唯一性校验，正则 ^[a-zA-Z0-9_-]{3,50}$", "example": "user@example.com"},
          {"field": "密码", "type": "string", "required": true, "constraints": "长度≥6位", "example": "pass123456"}
        ],
        "acceptance_criteria": [
          {"id": "AC-LOGIN-001", "description": "正确账号密码可成功登录并跳转首页", "type": "functional"},
          {"id": "AC-LOGIN-002", "description": "错误密码提示明确且累计失败次数", "type": "functional"},
          {"id": "AC-LOGIN-003", "description": "连续5次失败后账号锁定30分钟", "type": "security"},
          {"id": "AC-LOGIN-004", "description": "锁定期间正确密码也无法登录", "type": "security"}
        ]
      },
      "atomicity_check": {"trigger": true, "result": true, "boundary": true},
      "test_hints": ["正确账号+正确密码→登录成功", "错误密码→提示+累计次数", "连续5次→锁定30分钟", "锁定后正确密码→提示已锁定", "锁定31分钟后→可登录", "空账号→校验提示", "空密码→校验提示"]
    }
  ],
  "dependencies": [
    {
      "source_fp_name": "用户登录",
      "target_fp_name": "权限校验",
      "relation_type": "depends_on",
      "description": "用户必须先登录才能进行权限校验"
    }
  ]
}

## 依赖关系说明
在 functional_points 之外，额外输出 dependencies 数组，描述功能点之间的业务依赖。
- depends_on: A 的前置条件是 B（B 完成 A 才能执行）
- data_flows: A 的输出数据是 B 的输入
- invariants: A 和 B 共享同一全局约束
- lifecycle_scenarios: A 和 B 属于同一端到端流程
- conflicts_with: A 和 B 互斥（不应同时生效）
**依赖关系是可选输出**：如果功能点之间没有明确的业务依赖，可以输出空数组。

## FP 数量参考
- 单功能需求（如"实现用户登录"）：1-2 个 FP
- 简单需求（如"用户注册+登录"）：2-3 个 FP
- 中等需求（如"用户管理模块"）：3-6 个 FP
- 复杂需求（如"电商后台"）：6-12 个 FP
- **宁少勿多**：一个 FP 的信息量应该饱满（sub_srr 内容充实），而不是拆成很多信息量稀薄的小 FP

## FP 类型说明
- workflow: 业务流程操作（用户交互类功能）
- constraint: 约束/校验规则（**仅当该规则是独立的全局约束时才单独提取**，如"所有接口必须鉴权"）
- calculation: 计算/公式（独立于流程的计算逻辑，如"运费计算"）
- api_behavior: API 接口行为（独立的后端接口功能）
- data_record: 数据记录/存储
- config: 可配置的系统参数"""

FP_INTEGRATED_USER_PROMPT = """请从以下需求中拆分所有功能点，同时输出交互层 SRR。

需求标题：{req_title}
所属模块：{modules}
产品：{product_name}

概要 SRR（需求级摘要）：
{overview_srr}

原始需求文档：
{raw_text}
{existing_fp_hint}

请严格按「系统提示词」中的规则同时完成两项输出：
1. **interaction_srr**：只保留跨 FP 的交互规则、共享约束、数据契约（内部规则不下放）
2. **functional_points**：每个 FP 附带完整的内部 sub_srr（流程、规则、约束、验收标准）

**核心约束**：
- 一个业务概念 = 一个 FP，不要把同一业务的正常流程、异常处理、校验拆成多个 FP
- 如果需求只描述一个业务功能（如"用户登录"），只需要输出 1 个 FP
- 宁少勿多，每个 FP 的信息量要饱满
- **business_rules.type 只能从以下值中选择**：validation/calculation/workflow/permission/security/business/constraint/mandatory/uniqueness/format/range/logic/data_integrity/field_validation/access_control/authorization/authentication/prerequisite/presentation/initialization/enumeration/data_format/relationship/duplication/session/timeout/rate_limit/notification/data_retention/audit/encryption/integration
- **如果提示词中列出「以上功能点已存在数据库中」，则这些名称对应的 FP 的 sub_srr 只需保留 name/fp_type/summary（不得重新生成内部规则），在 functional_points 中标记 \"reuse_existing\": true
{extra_constraint}
"""

# ============================================================================
# BP 提取提示词（旧版，保留兼容）
# ============================================================================
FP_EXTRACT_BP_SYSTEM_PROMPT = """你是一个业务点（BP）提取专家。输入是一份已解析的结构化需求 SRR JSON。

你的任务：从 SRR 中提取候选业务点（Business Point），粒度≈产品的子功能域，如"用户认证""订单管理"。

输出规则：
1. 每个 BP 含：name（简短描述）、summary（一句话说明该域做什么）、suggested_module（建议归属的模块 ID，从 modules 列表匹配）
2. BP 数量控制在 2-6 个，宁可少而精
3. 对于 SRR 中已明确的功能模块划分，优先沿用
4. 如果需求只有一个明确功能域，只输出 1 个 BP

输出格式（JSON）：
{"business_points": [{"name": "用户认证", "summary": "处理用户登录/注册/密码重置", "suggested_module_id": "MOD-xxx"}]}
"""

# ============================================================================
# FP 拆分提示词（含 R1-R4 规则）
# ============================================================================
FP_SPLIT_SYSTEM_PROMPT = """你是一个功能点（FP）拆分专家。输入是业务点 BP 和对应的 SRR 片段。

你的任务：在当前 BP 范围内，按以下粒度规则拆分功能点。

## 粒度判定规则（按优先级，冲突时高优先级覆盖低优先级）

R4（最高优先级）：需多步弹窗才完成的操作 → 仍视为同一个 FP
  例："修改密码"（旧密码→新密码→确认→验证码四步弹窗）→ 1 个 FP

R3：带独立触发条件的状态流转 → 独立 FP
  例："连续 N 次输错密码后账号锁定并定时解锁" → 独立 FP

R1：同一实体的 CRUD 各操作 → 每个操作 = 独立 FP
  例："创建用户""编辑用户""删除用户" → 各 1 个 FP

R2（最低优先级）：同一表单各字段格式校验 → 不独立拆分，作为测试数据行展开

## FP 原子性三要素
每个 FP 必须具备：
1. 明确触发操作：是什么操作触发的（用户点击/API 调用/定时任务）
2. 可验证业务结果：操作后有什么可观察的变化（数据变化/状态流转/提示信息）
3. 有限元素边界：输入参数有明确范围，不是无限组合

## 输出格式（JSON）
{
  "functional_points": [
    {
      "name": "简短名称",
      "fp_type": "workflow",  // 6 种之一
      "summary": "一句话描述",
      "happy_path": "正常流程步骤",
      "alternate_flows": "异常分支",
      "business_rules": ["相关规则"],
      "triggers": "触发条件",
      "expected_results": "预期结果",
      "boundary_elements": "边界元素",
      "atomicity_check": {"trigger": true, "result": true, "boundary": true},
      "test_hints": ["测试提示1"],
      "suggested_module_id": "建议归属模块"
    }
  ]
}

## FP 类型说明
- workflow: 业务流程操作
- constraint: 约束/校验规则
- calculation: 计算/公式
- api_behavior: API 接口行为
- data_record: 数据记录/存储
- config: 配置项
"""

# ============================================================================
# FP 关联分析提示词
# ============================================================================
FP_ASSOCIATE_SYSTEM_PROMPT = """你是一个功能点关联分析专家。输入是一组功能点列表（含 name/summary）。

你的任务：分析功能点之间的关联关系。

## 关联类型
- depends_on: A 的前置条件是 B（B 完成 A 才能执行）
- data_flows: A 的输出数据是 B 的输入
- invariants: A 和 B 共享同一全局约束
- lifecycle_scenarios: A 和 B 属于同一端到端流程
- conflicts_with: A 和 B 互斥（不应同时生效）

## 输出格式（JSON）
{
  "dependencies": [
    {"source_fp_name": "用户登录", "target_fp_name": "订单创建",
     "relation_type": "depends_on",
     "description": "订单创建需要用户先登录"}
  ]
}
"""

# ============================================================================
# BP 提取用户提示词模板
# ============================================================================
FP_EXTRACT_BP_USER_PROMPT = """请从以下需求 SRR 中提取候选业务点。

需求：{req_title}
关联模块：{modules}
SRR 片段（精简版）：
{srr_snippet}
"""

# ============================================================================
# FP 拆分用户提示词模板
# ============================================================================
FP_SPLIT_USER_PROMPT = """请在以下业务点范围内拆分功能点。

业务点：{bp_name} — {bp_summary}
需求：{req_title}

SRR 片段：
{srr_snippet}

请严格按 R1-R4 规则拆分，每个 FP 标注类型和原子性检查。"""

# ============================================================================
# FP 关联分析用户提示词模板
# ============================================================================
FP_ASSOCIATE_USER_PROMPT = """请分析以下功能点之间的关联关系。

需求：{req_title}

功能点列表：
{fp_list}
"""

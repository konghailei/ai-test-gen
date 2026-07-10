# 模板字段定义说明

> 本文档详细说明 `templates/` 目录下三个模板文件的所有字段含义、类型约束和使用示例。

---

## 目录

1. [requirement-template.json — 需求输入模板](#1-requirement-templatejson--需求输入模板)
2. [testcase-output-template.json — 用例输出模板](#2-testcase-output-templatejson--用例输出模板)
3. [knowledge-base-schema.json — 知识库 Schema](#3-knowledge-base-schemajson--知识库-schema)

---

## 1. requirement-template.json — 需求输入模板

本模板定义 AI 测试用例生成系统的**标准化需求输入格式**（SRR JSON），用于 ReqAgent 解析后的结构化输出。

### 顶层字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `$schema` | string | ✅ | JSON Schema 引用 URL，用于校验格式版本 |
| `template_version` | string | ✅ | 模板版本号，当前为 `1.0` |
| `template_name` | string | ✅ | 模板名称 |
| `description` | string | ✅ | 模板用途描述 |
| `fields` | array | ✅ | 字段定义列表，每个元素描述一个 SRR JSON 的字段 |

---

### `fields[]` 数组元素结构

每个 `fields[]` 元素描述 SRR JSON 中的一个字段，包含以下属性：

| 属性 | 类型 | 说明 |
|------|------|------|
| `path` | string | 字段路径，使用 `.` 表示嵌套（如 `flows.happy_path`） |
| `label` | string | 字段的中文标签名 |
| `type` | string | 字段类型（`string`、`enum`、`object`、`array` 等） |
| `required` | boolean | 是否必填 |
| `pattern` | string | 正则表达式校验规则（可选） |
| `example` | any | 示例值 |
| `description` | string | 字段详细说明 |
| `max_length` | integer | 最大长度限制（可选） |
| `enum` | array | 枚举值列表（当 type=enum 时） |
| `properties` | object | 对象类型的子属性定义（可选） |
| `item_schema` | object | 数组类型的元素 Schema 定义（可选） |

---

### 核心字段详细说明

#### `id` — 需求 ID

| 属性 | 值 |
|------|-----|
| **路径** | `id` |
| **类型** | `string` |
| **必填** | ✅ |
| **格式规则** | `^REQ-\d{4}-\d{3,}$` （REQ-年份-序号） |
| **示例** | `REQ-2024-001` |
| **说明** | 全局唯一需求标识，用于需求版本管理和追溯 |

---

#### `title` — 需求标题

| 属性 | 值 |
|------|-----|
| **路径** | `title` |
| **类型** | `string` |
| **必填** | ✅ |
| **最大长度** | 255 字符 |
| **示例** | `用户登录功能` |
| **说明** | 一句话概述需求内容，要求简洁明确 |

---

#### `priority` — 优先级

| 属性 | 值 |
|------|-----|
| **路径** | `priority` |
| **类型** | `enum` |
| **必填** | ✅ |
| **枚举值** | `P0` / `P1` / `P2` / `P3` |
| **说明** | `P0`: 核心功能/阻塞级；`P1`: 重要功能；`P2`: 一般功能；`P3`: 低优先级 |

---

#### `module` — 所属模块

| 属性 | 值 |
|------|-----|
| **路径** | `module` |
| **类型** | `string` |
| **必填** | ✅ |
| **示例** | `auth`、`payment`、`order`、`user` |
| **说明** | 功能模块标识，用于模块级用例管理和检索 |

---

#### `source` — 需求来源

| 属性 | 值 |
|------|-----|
| **路径** | `source` |
| **类型** | `object` |
| **必填** | ❌ |
| **子属性** | |
| `source.type` | 来源类型：`jira` / `prd` / `user_story` / `email` / `conversation` / `api_doc` |
| `source.url` | 来源 URL（如 JIRA 链接、PRD 文档链接） |
| `source.ticket_id` | 来源工单 ID（如 JIRA-1234） |
| **说明** | 记录需求的原始来源，便于追溯和批量重新生成 |

---

#### `summary` — 功能概述

| 属性 | 值 |
|------|-----|
| **路径** | `summary` |
| **类型** | `string` |
| **必填** | ✅ |
| **最大长度** | 500 字符 |
| **示例** | `用户通过账号密码登录系统，登录成功后跳转首页，支持验证码和账号锁定机制` |
| **说明** | 用 1-3 句话描述需求的核心功能，AI 据此理解需求主旨 |

---

#### `preconditions` — 前置条件

| 属性 | 值 |
|------|-----|
| **路径** | `preconditions` |
| **类型** | `array<string>` |
| **必填** | ❌ |
| **元素最大长度** | 200 字符 |
| **示例** | `["用户已在系统注册", "账号未处于锁定状态", "系统处于正常运行状态"]` |
| **说明** | 执行该需求功能前必须满足的条件，直接转化为测试用例的前置条件 |

---

#### `flows.happy_path` — 正常流程

| 属性 | 值 |
|------|-----|
| **路径** | `flows.happy_path` |
| **类型** | `array<Step>` |
| **必填** | ✅ |
| **元素 Schema** | |
| `Step.order` | 步骤序号（integer） |
| `Step.action` | 用户操作描述（string，最大 500 字符） |
| `Step.expect` | 预期系统响应（string，最大 500 字符） |
| **说明** | 描述用户与系统交互的主成功场景，是生成功能测试用例的主要依据 |

**示例：**
```json
"happy_path": [
  {"order": 1, "action": "打开登录页面", "expect": "显示用户名、密码输入框和登录按钮"},
  {"order": 2, "action": "输入正确的用户名和密码", "expect": "输入框正常显示内容"},
  {"order": 3, "action": "点击登录按钮", "expect": "登录成功，跳转首页"}
]
```

---

#### `flows.alternate_flows` — 异常/分支流程

| 属性 | 值 |
|------|-----|
| **路径** | `flows.alternate_flows` |
| **类型** | `array<AlternateFlow>` |
| **必填** | ❌ |
| **元素 Schema** | |
| `AlternateFlow.name` | 分支流程名称（如"密码错误"） |
| `AlternateFlow.trigger` | 触发条件（如"输入错误密码"） |
| `AlternateFlow.steps` | 步骤描述列表（array<string>） |
| `AlternateFlow.expect` | 预期结果 |
| **说明** | 描述异常路径或分支场景，直接转化为异常测试用例 |

---

#### `boundary_conditions` — 边界条件

| 属性 | 值 |
|------|-----|
| **路径** | `boundary_conditions` |
| **类型** | `array<BoundaryCondition>` |
| **必填** | ❌ |
| **元素 Schema** | |
| `BoundaryCondition.field` | 涉及字段名（如"密码"） |
| `BoundaryCondition.condition` | 边界条件描述（如"最少6位"） |
| `BoundaryCondition.expect` | 预期行为 |
| **说明** | 明确声明的边界条件，AI 据此生成边界值测试用例 |

---

#### `business_rules` — 业务规则

| 属性 | 值 |
|------|-----|
| **路径** | `business_rules` |
| **类型** | `array<BusinessRule>` |
| **必填** | ❌ |
| **元素 Schema** | |
| `BusinessRule.id` | 规则 ID（格式 `BR-XX`，如 `BR-01`） |
| `BusinessRule.rule` | 规则描述 |
| `BusinessRule.type` | 规则类型：`security` / `validation` / `workflow` / `calculation` / `notification` |
| **说明** | 业务约束规则，AI 据此生成安全测试和验证测试用例 |

---

#### `data_constraints` — 数据约束

| 属性 | 值 |
|------|-----|
| **路径** | `data_constraints` |
| **类型** | `array<DataConstraint>` |
| **必填** | ❌ |
| **元素 Schema** | |
| `DataConstraint.field` | 字段名 |
| `DataConstraint.type` | 数据类型：`string` / `int` / `float` / `boolean` / `datetime` / `enum` / `json` |
| `DataConstraint.required` | 是否必填（boolean） |
| `DataConstraint.constraints` | 约束描述（如"长度6-20位，必须包含字母和数字"） |
| `DataConstraint.example` | 示例值（object，如 `{"valid": "abc123", "invalid": "abc"}`） |
| **说明** | 输入数据的格式和约束，AI 据此生成数据验证测试用例 |

---

#### `non_functional` — 非功能需求

| 属性 | 值 |
|------|-----|
| **路径** | `non_functional` |
| **类型** | `object` |
| **必填** | ❌ |
| **子属性** | |
| `non_functional.performance` | 性能要求（如"登录接口 P99 < 500ms"） |
| `non_functional.security` | 安全要求（如"密码必须 BCrypt 加密"） |
| `non_functional.compatibility` | 兼容性要求 |
| `non_functional.availability` | 可用性要求（如"99.9% SLA"） |
| `non_functional.scalability` | 可扩展性要求 |
| **说明** | 非功能需求，AI 据此生成性能/安全/兼容性测试用例 |

---

#### `relationships` — 关联需求

| 属性 | 值 |
|------|-----|
| **路径** | `relationships` |
| **类型** | `array<Relationship>` |
| **必填** | ❌ |
| **元素 Schema** | |
| `Relationship.target` | 关联需求 ID |
| `Relationship.type` | 关系类型：`depends_on`（依赖）/ `extends`（扩展）/ `conflicts_with`（冲突）/ `related_to`（相关） |
| `Relationship.strength` | 关联强度：`strong` / `medium` / `weak` |
| `Relationship.note` | 备注说明 |
| **说明** | 需求间的依赖和关联关系，用于影响分析和回归测试范围判断 |

---

#### `tags` — 标签

| 属性 | 值 |
|------|-----|
| **路径** | `tags` |
| **类型** | `array<string>` |
| **必填** | ❌ |
| **示例** | `["login", "auth", "security", "P0"]` |
| **说明** | 用于需求分类和检索的标签，支持按标签批量生成用例 |

---

## 2. testcase-output-template.json — 用例输出模板

本模板定义 AI 生成的测试用例的**标准化输出格式**（UTCF — Universal Test Case Format）。

### `metadata` — 生成批次元信息

| 字段 | 类型 | 说明 |
|------|------|------|
| `metadata.generated_at` | string | 生成时间戳（ISO 8601） |
| `metadata.generator` | string | 生成工具名称和版本 |
| `metadata.generator_model` | string | 使用的 LLM 模型名（如 `deepseek-v4`、`gpt-4o`） |
| `metadata.source_requirement.id` | string | 来源需求 ID |
| `metadata.source_requirement.title` | string | 来源需求标题 |
| `metadata.source_requirement.version` | integer | 来源需求版本号 |
| `metadata.gen_batch_id` | string | 本次生成的批次 ID（用于追溯） |
| `metadata.total_cases` | integer | 本次生成的用例总数 |
| `metadata.coverage_summary` | object | 各类型用例数量统计 |

#### `coverage_summary` 详细说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `functional` | integer | 功能测试用例数量 |
| `boundary` | integer | 边界值测试用例数量 |
| `exception` | integer | 异常测试用例数量 |
| `security` | integer | 安全测试用例数量 |
| `performance` | integer | 性能测试用例数量 |
| `integration` | integer | 集成测试用例数量 |
| `exploratory` | integer | 探索性测试用例数量 |

---

### `test_suite[]` — 测试用例数组

每个测试用例对象的字段说明：

#### 基础信息

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | ✅ | 用例唯一 ID，格式：`TC-{模块}-{需求序号}-{用例序号}`（如 `TC-AUTH-0001-0001`） |
| `title` | string | ✅ | 用例标题，简洁描述测试目的 |
| `description` | string | ✅ | 用例详细描述，说明要验证什么 |
| `type` | string | ✅ | 用例类型：`functional` / `boundary` / `exception` / `security` / `performance` / `integration` / `exploratory` |
| `priority` | string | ✅ | 优先级：`P0` / `P1` / `P2` / `P3`（继承自需求） |
| `tags` | array<string> | ❌ | 用例标签，用于分类和筛选（如 `smoke`、`regression`、`security`） |

---

#### 测试步骤

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `preconditions` | array<string> | ❌ | 执行本用例前必须满足的条件 |
| `steps` | array<Step> | ✅ | 测试步骤列表 |

##### `steps[]` 元素结构

| 字段 | 类型 | 说明 |
|------|------|------|
| `Step.step` | integer | 步骤序号 |
| `Step.action` | string | 测试操作步骤描述 |
| `Step.expected` | string | 该步骤的预期结果 |
| `Step.data` | object | 该步骤使用的测试数据（可选） |

---

#### 测试数据

| 字段 | 类型 | 说明 |
|------|------|------|
| `test_data` | object | 本用例使用的所有测试数据，以键值对形式组织，便于自动化脚本读取 |

---

#### 断言

| 字段 | 类型 | 说明 |
|------|------|------|
| `assertions` | array<Assertion> | 测试断言列表，定义如何验证测试结果 |

##### `assertions[]` 元素结构

| 字段 | 类型 | 说明 |
|------|------|------|
| `Assertion.type` | string | 断言类型，见下方"断言类型说明" |
| `Assertion.target` | string | 断言目标（如 `response.status_code`、`response.token`） |
| `Assertion.expected_value` | any | 期望值 |
| `Assertion.message` | string | 断言失败时的错误信息 |

##### 断言类型说明

| 类型 | 说明 | 示例 |
|------|------|------|
| `status_code` | 验证 HTTP 状态码 | `{"type": "status_code", "expected_value": 200}` |
| `equals` | 精确匹配 | `{"type": "equals", "target": "response.code", "expected_value": 0}` |
| `not_equals` | 不等于 | `{"type": "not_equals", "target": "response.status", "expected_value": 500}` |
| `contains` | 包含子串 | `{"type": "contains", "target": "error.message", "expected_value": "锁定"}` |
| `not_contains` | 不包含子串 | |
| `not_null` | 不为空 | `{"type": "not_null", "target": "response.token"}` |
| `is_null` | 为空 | |
| `gt` / `gte` | 大于 / 大于等于 | `{"type": "gt", "target": "metrics.latency_ms", "expected_value": 1000}` |
| `lt` / `lte` | 小于 / 小于等于 | |
| `length_equals` | 长度等于 | |
| `length_gt` | 长度大于 | |
| `regex_match` | 正则匹配 | |
| `json_schema` | JSON Schema 校验 | |

---

#### 执行控制

| 字段 | 类型 | 说明 |
|------|------|------|
| `execution.timeout_seconds` | integer | 用例执行超时时间（秒） |
| `execution.retry_count` | integer | 失败重试次数（0 = 不重试） |
| `execution.depends_on` | array<string> | 依赖的用例 ID 列表（需先执行这些用例） |
| `execution.skip_conditions` | array<string> | 跳过执行的条件（如 `["env == 'dev'", "WAIT_30_MINUTES"]`） |

---

#### 追溯与质量

| 字段 | 类型 | 说明 |
|------|------|------|
| `cleanup` | array<string> | 测试后的清理操作列表（如删除测试数据、重置状态） |
| `related_requirement` | string | 关联的需求 ID（用于追溯矩阵） |
| `source_pattern` | string | 生成时参考的测试模式 ID（如 `auth_pattern`） |
| `confidence` | float | **最终置信度**（0.0-1.0），由 ReviewAgent.calculate_confidence 计算后写入，覆盖 GenAgent 的自评估。用于人工审核优先级排序、知识库入库权重。详见 [doc-03 §5.2](../docs/03-case-generation-flow.md#52-置信度打分) |
| `gen_metadata.gen_confidence` | float | GenAgent 生成阶段 LLM 自评估置信度（0.0-1.0），仅作为 ReviewAgent 计算的输入因子之一（权重 0.1），不对外暴露 |

---

## 3. knowledge-base-schema.json — 知识库 Schema

本模板定义知识库的**数据模型**，包括 Vector DB（向量数据库）和 Relational DB（关系型数据库）的 Schema。

---

### `vector_db` — 向量数据库 Schema

定义 ChromaDB / Milvus 等向量数据库的 Collection 结构。

#### 通用字段说明

每个 Collection 包含以下通用属性：

| 属性 | 类型 | 说明 |
|------|------|------|
| `name` | string | Collection 名称 |
| `description` | string | Collection 用途描述 |
| `embedding_model` | string | 使用的嵌入模型名称 |
| `embedding_dim` | integer | 嵌入向量维度 |
| `distance_metric` | string | 相似度计算方式：`cosine`（余弦）/ `l2`（欧氏距离）/ `ip`（内积） |
| `metadata_schema` | object | 存储在向量 DB 中的元数据字段定义 |
| `text_template` | string | 用于生成嵌入向量的文本模板（将结构化字段拼接为自然语言） |

---

#### `requirements` Collection — 需求向量存储

存储历史需求文档的向量嵌入，用于相似需求检索。

| 元数据字段 | 类型 | 说明 |
|-----------|------|------|
| `req_id` | string | 需求 ID（对应 `requirements.id`） |
| `doc_type` | const | 固定值 `'requirement'`，用于过滤 |
| `module` | string | 所属模块，用于模块级过滤 |
| `priority` | enum | 优先级，用于优先级过滤 |
| `version` | integer | 需求版本号 |
| `status` | enum | `active`（有效）/ `superseded`（被新版本替代）/ `deprecated`（废弃） |
| `tags` | array<string> | 标签，用于标签过滤 |
| `created_at` | datetime | 创建时间（ISO 8601） |
| `updated_at` | datetime | 最后更新时间 |

**`text_template` 说明：**
将需求的结构化字段拼接为自然语言文本，用于生成嵌入向量：
```
{title}\n{summary}\n前置条件: {preconditions}\n正常流程: {happy_path}\n异常流程: {alternate_flows}\n业务规则: {business_rules}\n测试关注点: {test_points_hint}
```

---

#### `test_cases` Collection — 测试用例向量存储

存储历史测试用例的向量嵌入，用于相似用例检索和模式复用。

| 元数据字段 | 类型 | 说明 |
|-----------|------|------|
| `case_id` | string | 用例 ID（对应 `test_cases.id`） |
| `req_id` | string | 来源需求 ID |
| `doc_type` | const | 固定值 `'test_case'` |
| `case_type` | enum | 用例类型：`functional` / `boundary` / `exception` / `security` / `performance` / `integration` |
| `priority` | enum | 优先级 |
| `module` | string | 所属模块（主模块，等于 modules[0]） |
| `status` | enum | `draft`（草稿）/ `reviewed`（已审核）/ `approved`（已批准）/ `rejected`（已驳回） |
| `confidence` | float | 最终置信度（0.0-1.0），由 ReviewAgent 计算；与 UTCF 顶层 `confidence` 同源 |
| `acceptance_rate` | float | 人工接受率（被采纳的用例数 / 总生成数） |
| `source_pattern` | string | 参考的测试模式 ID（可选） |
| `weight` | float | 检索权重（被采纳→+0.1，被驳回→-0.2，执行通过→+0.05） |

---

#### `test_patterns` Collection — 测试模式向量存储

存储预定义的测试模式（种子数据），用于冷启动阶段的 Few-shot 示例。

| 元数据字段 | 类型 | 说明 |
|-----------|------|------|
| `pattern_id` | integer | 模式 ID |
| `pattern_name` | string | 模式名称（如 `CRUD模式`、`状态机模式`） |
| `doc_type` | const | 固定值 `'test_pattern'` |
| `category` | enum | 模式分类：`crud` / `state_machine` / `auth` / `validation` / `workflow` / `concurrency` / `async` / `consistency` |
| `usage_count` | integer | 被使用次数（越多表示越有效） |
| `success_rate` | float | 使用此模式生成的用例通过率 |

**种子模式列表：**
- `crud_pattern` — CRUD 增删改查模式
- `state_machine_pattern` — 状态机转移模式
- `auth_pattern` — 认证授权模式
- `validation_pattern` — 输入校验模式
- `pagination_pattern` — 分页排序模式
- `concurrency_pattern` — 并发安全模式
- `async_callback_pattern` — 异步回调模式
- `data_consistency_pattern` — 数据一致性模式

---

#### `error_cases` Collection — 错误案例向量存储

存储 AI 生成失败或被驳回的用例，用于避免重复错误。

| 元数据字段 | 类型 | 说明 |
|-----------|------|------|
| `error_id` | integer | 错误记录 ID |
| `original_case_id` | string | 原始用例 ID |
| `doc_type` | const | 固定值 `'error_case'` |
| `error_type` | enum | 错误类型：`missing_precondition`（缺少前置条件）/ `wrong_boundary`（边界值错误）/ `redundant`（冗余用例）/ `ambiguous`（描述模糊）/ `incomplete`（不完整）/ `conflicting`（与需求冲突） |
| `created_at` | datetime | 记录创建时间 |

---

### `relational_db` — 关系型数据库 Schema

定义 PostgreSQL 的表结构，用于结构化数据的精确查询和管理。

#### `requirements` 表 — 需求元数据

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `id` | VARCHAR(32) | PK | 需求 ID（同 SRR JSON 的 `id`） |
| `title` | VARCHAR(255) | NOT NULL | 需求标题 |
| `priority` | VARCHAR(4) | CHECK | 优先级（P0/P1/P2/P3） |
| `module` | VARCHAR(64) | NOT NULL | 所属模块 |
| `tags` | TEXT[] | DEFAULT '{}' | 标签数组 |
| `version` | INTEGER | DEFAULT 1 | 版本号（每次变更 +1） |
| `status` | VARCHAR(16) | DEFAULT 'active' | 状态：`active` / `superseded` / `deprecated` |
| `srr_json` | JSONB | NOT NULL | SRR JSON 完整数据 |
| `raw_text` | TEXT | | 原始需求文本（未解析前） |
| `source_type` | VARCHAR(32) | | 来源类型（jira/prd/user_story 等） |
| `source_url` | TEXT | | 来源 URL |
| `created_by` | VARCHAR(64) | | 创建人 |
| `created_at` | TIMESTAMPTZ | DEFAULT NOW() | 创建时间 |
| `updated_at` | TIMESTAMPTZ | DEFAULT NOW() | 更新时间 |
| `superseded_by` | VARCHAR(32) | FK | 被哪个新版本替代（指向同表 `id`） |

**索引：**
- `idx_req_module` — 按模块查询加速
- `idx_req_priority` — 按优先级查询加速
- `idx_req_status` — 按状态过滤加速
- `idx_req_tags` — 标签 GIN 索引（支持数组包含查询）
- `idx_req_srr` — `srr_json` 的 GIN 索引（支持 JSON 字段内查询）

---

#### `test_cases` 表 — 测试用例元数据

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `id` | VARCHAR(32) | PK | 用例 ID（同 UTCF 的 `id`） |
| `req_id` | VARCHAR(32) | FK | 来源需求 ID（→ `requirements.id`） |
| `title` | VARCHAR(255) | NOT NULL | 用例标题 |
| `type` | VARCHAR(32) | NOT NULL | 用例类型 |
| `priority` | VARCHAR(4) | | 优先级 |
| `status` | VARCHAR(16) | DEFAULT 'draft' | 状态：`draft` / `reviewed` / `approved` / `rejected` |
| `case_json` | JSONB | NOT NULL | UTCF 完整数据 |
| `source` | VARCHAR(32) | DEFAULT 'ai_generated' | 来源：`ai_generated` / `human_created` / `imported` |
| `gen_batch_id` | VARCHAR(64) | | 生成批次 ID（→ `generation_batches.id`） |
| `confidence` | NUMERIC(3,2) | | 最终置信度（0.00-1.00），由 ReviewAgent.calculate_confidence 计算；与 UTCF 顶层 `confidence` 同源 |
| `reviewed_by` | VARCHAR(64) | | 审核人 |
| `reviewed_at` | TIMESTAMPTZ | | 审核时间 |
| `feedback` | JSONB | | 审核反馈（结构：`{accepted: bool, comments: string, corrections: json}`） |
| `execution_count` | INTEGER | DEFAULT 0 | 执行次数（用于统计用例使用频率） |
| `last_pass_rate` | NUMERIC(3,2) | | 最近一次执行的通过率 |
| `weight` | NUMERIC(4,3) | DEFAULT 1.0 | 检索权重（用于知识库检索排序） |
| `created_at` | TIMESTAMPTZ | DEFAULT NOW() | 创建时间 |
| `updated_at` | TIMESTAMPTZ | DEFAULT NOW() | 更新时间 |

**索引：**
- `idx_case_req` — 按需求 ID 查询加速
- `idx_case_type` — 按类型过滤加速
- `idx_case_status` — 按状态过滤加速
- `idx_case_confidence` — 按置信度排序加速
- `idx_case_gen_batch` — 按批次查询加速
- `idx_case_weight` — 按权重降序查询加速

---

#### `test_patterns` 表 — 测试模式库

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `id` | SERIAL | PK | 自增主键 |
| `name` | VARCHAR(128) | NOT NULL UNIQUE | 模式名称 |
| `category` | VARCHAR(32) | NOT NULL | 模式分类 |
| `pattern_json` | JSONB | NOT NULL | 模式完整定义（包含模板步骤、断言模板等） |
| `usage_count` | INTEGER | DEFAULT 0 | 被使用次数 |
| `success_rate` | NUMERIC(3,2) | | 成功率（使用此模式生成的用例通过率） |
| `created_at` | TIMESTAMPTZ | DEFAULT NOW() | 创建时间 |

---

#### `error_cases` 表 — 错误案例库

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `id` | SERIAL | PK | 自增主键 |
| `original_case_id` | VARCHAR(32) | FK | 原始用例 ID（→ `test_cases.id`） |
| `error_type` | VARCHAR(32) | NOT NULL | 错误类型 |
| `error_detail` | TEXT | | 错误详细描述 |
| `feedback` | TEXT | | 人工反馈意见 |
| `corrected_json` | JSONB | | 修正后的 UTCF JSON（用于学习） |
| `created_at` | TIMESTAMPTZ | DEFAULT NOW() | 记录时间 |

---

#### `generation_batches` 表 — 生成批次记录

记录每次批量生成的元信息，用于成本统计和效果分析。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `id` | VARCHAR(64) | PK | 批次 ID（UUID 或时间戳） |
| `req_id` | VARCHAR(32) | FK | 对应需求 ID |
| `total_cases` | INTEGER | | 生成用例总数 |
| `accepted_cases` | INTEGER | DEFAULT 0 | 被接受用例数 |
| `rejected_cases` | INTEGER | DEFAULT 0 | 被驳回用例数 |
| `model_used` | VARCHAR(64) | | 使用的 LLM 模型 |
| `prompt_version` | VARCHAR(16) | | Prompt 模板版本 |
| `total_tokens` | INTEGER | | 消耗 Token 总数 |
| `total_cost` | NUMERIC(10,4) | | 消耗费用（USD） |
| `duration_ms` | INTEGER | | 生成耗时（毫秒） |
| `created_at` | TIMESTAMPTZ | DEFAULT NOW() | 生成时间 |

---

## 附录：字段命名规范

### ID 格式规范

| 实体 | 格式 | 示例 |
|------|------|------|
| 需求 ID | `REQ-YYYY-NNN` | `REQ-2024-001` |
| 用例 ID | `TC-{模块}-{需求序号}-{用例序号}` | `TC-AUTH-0001-0001` |
| 批次 ID | `BATCH-{时间戳}-{随机串}` | `BATCH-20240624T103000-Z8X7` |
| 业务规则 ID | `BR-XX` | `BR-01` |

### 枚举值规范

| 枚举类型 | 可选值 |
|---------|--------|
| `priority` | `P0`（核心）/ `P1`（重要）/ `P2`（一般）/ `P3`（低优） |
| `case_type` | `functional` / `boundary` / `exception` / `security` / `performance` / `integration` / `exploratory` |
| `case_status` | `draft` / `reviewed` / `approved` / `rejected` |
| `req_status` | `active` / `superseded` / `deprecated` |
| `error_type` | `missing_precondition` / `wrong_boundary` / `redundant` / `ambiguous` / `incomplete` / `conflicting` |
| `rule_type` | `security` / `validation` / `workflow` / `calculation` / `notification` |
| `relationship_type` | `depends_on` / `extends` / `conflicts_with` / `related_to` |

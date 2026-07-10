# 04 — 用例输出格式规范

> 定义标准化输出格式，实现"回写知识库 + 对接测试框架"双闭环

---

## 1. 设计目标

测试用例的输出格式需要满足三个相互制约的需求：

| 需求 | 具体目标 |
|------|---------|
| **AI 可生成** | LLM 能稳定、低错误率地输出此格式 |
| **知识库可存储** | 可直接作为知识库的存储原语，支持检索和复用 |
| **框架可执行** | 能被转换为 pytest/JUnit/Gherkin/Robot Framework 等测试框架的输入 |

**核心设计决策**：采用 **通用中间格式 (Universal Test Case Format, UTCF)** + **适配转换层** 的方案。

---

## 2. 通用测试用例格式 (UTCF)

### 2.1 JSON Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://ai-test-gen/utcf-v1.0.json",
  "title": "Universal Test Case Format v1.0",
  "type": "object",
  "required": ["format_version", "metadata", "test_suite"],
  "properties": {
    "format_version": {
      "type": "string",
      "const": "1.0",
      "description": "UTC 格式版本号"
    },
    "metadata": {
      "type": "object",
      "required": ["generated_at", "generator", "source_requirement"],
      "properties": {
        "generated_at": {"type": "string", "format": "date-time"},
        "generator": {"type": "string", "description": "生成器标识，如 'ai-test-gen v1.2.0'"},
        "generator_model": {"type": "string", "description": "LLM 模型，如 'deepseek-v4'"},
        "source_requirement": {
          "type": "object",
          "required": ["id", "version"],
          "properties": {
            "id": {"type": "string"},
            "title": {"type": "string"},
            "version": {"type": "integer"}
          }
        },
        "gen_batch_id": {"type": "string", "description": "批次 ID，同批生成共享"},
        "total_cases": {"type": "integer"},
        "coverage_summary": {
          "type": "object",
          "properties": {
            "functional": {"type": "integer"},
            "boundary": {"type": "integer"},
            "exception": {"type": "integer"},
            "security": {"type": "integer"},
            "performance": {"type": "integer"}
          }
        }
      }
    },
    "test_suite": {
      "type": "array",
      "items": {"$ref": "#/definitions/TestCase"}
    }
  },
  "definitions": {
    "TestCase": {
      "type": "object",
      "required": ["id", "title", "type", "priority", "steps"],
      "properties": {
        "id": {
          "type": "string",
          "pattern": "^TC-[A-Z]+-\\d{4}-\\d{4}$",
          "description": "用例唯一 ID: TC-{MODULE}-{REQ_SEQ}-{CASE_SEQ}"
        },
        "title": {
          "type": "string",
          "description": "简洁的用例标题"
        },
        "description": {
          "type": "string",
          "description": "用例详细描述，说明测试目的"
        },
        "type": {
          "type": "string",
          "enum": ["functional", "boundary", "exception", "security", "performance", "integration", "exploratory"]
        },
        "priority": {
          "type": "string",
          "enum": ["P0", "P1", "P2", "P3"]
        },
        "tags": {
          "type": "array",
          "items": {"type": "string"},
          "description": "标签: smoke, regression, slow, flaky 等"
        },
        "preconditions": {
          "type": "array",
          "items": {"type": "string"},
          "description": "执行前必须满足的条件"
        },
        "steps": {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["step", "action", "expected"],
            "properties": {
              "step": {"type": "integer"},
              "action": {"type": "string", "description": "操作描述"},
              "expected": {"type": "string", "description": "预期结果"},
              "data": {
                "type": "object",
                "description": "本步骤关联的测试数据"
              }
            }
          }
        },
        "test_data": {
          "type": "object",
          "description": "全局测试数据集"
        },
        "assertions": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "type": {"type": "string", "enum": ["equals", "contains", "matches", "status_code", "gt", "lt", "not_null", "custom"]},
              "target": {"type": "string"},
              "expected_value": {},
              "message": {"type": "string"}
            }
          }
        },
        "cleanup": {
          "type": "array",
          "items": {"type": "string"},
          "description": "用例执行后的清理操作"
        },
        "related_requirement": {
          "type": "string",
          "description": "关联需求 ID"
        },
        "source_pattern": {
          "type": "string",
          "description": "参考的测试模式名称"
        },
        "confidence": {
          "type": "number",
          "minimum": 0,
          "maximum": 1,
          "description": "AI 生成置信度"
        },
        "execution": {
          "type": "object",
          "description": "执行配置（对接测试框架用）",
          "properties": {
            "timeout_seconds": {"type": "integer", "default": 30},
            "retry_count": {"type": "integer", "default": 0},
            "depends_on": {
              "type": "array",
              "items": {"type": "string"},
              "description": "依赖的前置用例 ID"
            },
            "skip_conditions": {
              "type": "array",
              "items": {"type": "string"}
            }
          }
        }
      }
    }
  }
}
```

### 2.2 完整示例

```json
{
  "format_version": "1.0",
  "metadata": {
    "generated_at": "2024-06-24T16:30:00Z",
    "generator": "ai-test-gen v1.2.0",
    "generator_model": "deepseek-v4",
    "source_requirement": {
      "id": "REQ-2024-001",
      "title": "用户登录功能",
      "version": 1
    },
    "gen_batch_id": "batch-20240624-001",
    "total_cases": 12,
    "coverage_summary": {
      "functional": 4,
      "boundary": 3,
      "exception": 2,
      "security": 2,
      "performance": 1
    }
  },
  "test_suite": [
    {
      "id": "TC-AUTH-0001-0001",
      "title": "正常登录流程验证",
      "type": "functional",
      "priority": "P0",
      "tags": ["smoke", "regression"],
      "preconditions": [
        "测试账号 test_user 已在系统中注册",
        "账号未处于锁定状态"
      ],
      "steps": [
        {"step": 1, "action": "打开登录页面", "expected": "页面正常加载，显示用户名、密码输入框和登录按钮"},
        {"step": 2, "action": "输入用户名 'test_user'", "expected": "输入框显示输入的用户名"},
        {"step": 3, "action": "输入密码 'Test@123456'", "expected": "密码以密文形式显示"},
        {"step": 4, "action": "点击登录按钮", "expected": "登录成功，页面跳转到首页，顶部显示用户名"}
      ],
      "test_data": {
        "username": "test_user",
        "password": "Test@123456"
      },
      "assertions": [
        {"type": "status_code", "expected_value": 200, "message": "登录接口返回 200"},
        {"type": "not_null", "target": "response.token", "message": "返回 JWT Token 不为空"},
        {"type": "equals", "target": "response.redirect_url", "expected_value": "/home", "message": "跳转到首页"}
      ],
      "cleanup": ["退出登录", "清理 Session"],
      "related_requirement": "REQ-2024-001",
      "source_pattern": "auth_pattern",
      "confidence": 0.95,
      "execution": {
        "timeout_seconds": 15,
        "retry_count": 0,
        "depends_on": []
      }
    },
    {
      "id": "TC-AUTH-0001-0002",
      "title": "密码错误三次后验证码出现",
      "type": "functional",
      "priority": "P1",
      "preconditions": [
        "测试账号 test_user 已在系统中注册",
        "账号未处于锁定状态"
      ],
      "steps": [
        {"step": 1, "action": "连续 3 次输入错误密码", "expected": "每次提示「密码错误」，第 3 次后出现验证码输入框"},
        {"step": 2, "action": "不输入验证码直接登录", "expected": "提示「请输入验证码」"},
        {"step": 3, "action": "输入错误验证码", "expected": "提示「验证码错误」"},
        {"step": 4, "action": "输入正确密码 + 正确验证码", "expected": "登录成功"}
      ],
      "test_data": {
        "username": "test_user",
        "wrong_passwords": ["Wrong1", "Wrong2", "Wrong3"],
        "correct_password": "Test@123456"
      },
      "assertions": [
        {"type": "contains", "target": "response.message", "expected_value": "验证码", "message": "第3次错误后出现验证码提示"}
      ],
      "cleanup": ["重置失败计数"],
      "related_requirement": "REQ-2024-001",
      "confidence": 0.88,
      "execution": {"timeout_seconds": 20}
    },
    {
      "id": "TC-AUTH-0001-0003",
      "title": "密码长度边界值测试 — 最小长度 6",
      "type": "boundary",
      "priority": "P2",
      "preconditions": ["测试账号 test_user 已注册"],
      "steps": [
        {"step": 1, "action": "输入密码长度=5", "expected": "前端校验拦截，提示「密码长度至少6位」，不允许提交"},
        {"step": 2, "action": "输入密码长度=6", "expected": "允许提交，登录成功"},
        {"step": 3, "action": "输入密码长度=128", "expected": "允许提交"},
        {"step": 4, "action": "输入密码长度=129", "expected": "前端校验拦截，提示「密码过长」"}
      ],
      "test_data": {
        "username": "test_user",
        "password_5": "12345",
        "password_6": "123456",
        "password_128": "a" * 128,
        "password_129": "a" * 129
      },
      "assertions": [
        {"type": "contains", "target": "error.message", "expected_value": "长度", "message": "超边界时提示长度错误"}
      ],
      "cleanup": [],
      "related_requirement": "REQ-2024-001",
      "source_pattern": "validation_pattern",
      "confidence": 0.92,
      "execution": {"timeout_seconds": 10}
    }
  ]
}
```

---

## 3. 与 Gherkin 的双向转换

### 3.1 UTCF → Gherkin

```python
def utcf_to_gherkin(utcf_case: dict) -> str:
    """将 UTCF 用例转换为 Gherkin Feature 格式"""
    lines = []
    lines.append(f"Feature: {utcf_case['related_requirement']}")
    lines.append(f"  Scenario: {utcf_case['title']}")
    lines.append(f"    # Priority: {utcf_case['priority']} | Type: {utcf_case['type']}")
    
    for pc in utcf_case.get("preconditions", []):
        lines.append(f"    Given {pc}")
    
    for step in utcf_case["steps"]:
        if step["step"] == 1 and not utcf_case.get("preconditions"):
            lines.append(f"    Given {step['action']}")
        else:
            lines.append(f"    When {step['action']}")
        lines.append(f"    Then {step['expected']}")
    
    return "\n".join(lines)
```

**输出示例**：

```gherkin
Feature: REQ-2024-001 用户登录功能
  Scenario: 正常登录流程验证
    # Priority: P0 | Type: functional
    Given 测试账号 test_user 已在系统中注册
    Given 账号未处于锁定状态
    When 打开登录页面
    Then 页面正常加载，显示用户名、密码输入框和登录按钮
    When 输入用户名 'test_user'
    Then 输入框显示输入的用户名
    When 输入密码 'Test@123456'
    Then 密码以密文形式显示
    When 点击登录按钮
    Then 登录成功，页面跳转到首页，顶部显示用户名
```

### 3.2 Gherkin → UTCF

支持从已有 Gherkin 文件反向解析为 UTCF，用于导入历史资产：

```python
def gherkin_to_utcf(gherkin_text: str, req_id: str) -> dict:
    """反向转换，用于知识库导入"""
    # 使用 gherkin-parser 库解析
    # 提取 Scenario、Given/When/Then 到 UTCF steps
    ...
```

---

## 4. 测试框架适配层

```
                     UTCF JSON
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│ pytest       │ │ JUnit/TestNG │ │ Robot        │
│ Adapter      │ │ Adapter      │ │ Framework    │
│              │ │              │ │ Adapter      │
├──────────────┤ ├──────────────┤ ├──────────────┤
│utcf→pytest   │ │utcf→Java     │ │utcf→Robot    │
│function      │ │@Test method  │ │Keyword        │
└──────────────┘ └──────────────┘ └──────────────┘
```

### 4.1 pytest 适配器

```python
# utcf_pytest_adapter.py
import pytest
import json

def load_utcf(filepath: str) -> list[dict]:
    with open(filepath) as f:
        data = json.load(f)
    return data["test_suite"]

def utcf_case_to_pytest(case: dict) -> str:
    """将单个 UTCF 用例转为 pytest 函数"""
    func_name = case["id"].lower().replace("-", "_")
    
    lines = []
    # 装饰器
    tags = ",".join(case.get("tags", []))
    if tags:
        lines.append(f'@pytest.mark.{case["type"]}')
    
    lines.append(f'def {func_name}():')
    lines.append(f'    """{case["title"]}"""')
    
    # 前置条件 → setup 代码
    for pc in case.get("preconditions", []):
        lines.append(f"    # Given: {pc}")
    
    # 步骤
    for step in case["steps"]:
        lines.append(f"    # Step {step['step']}: {step['action']}")
        lines.append(f"    # Expected: {step['expected']}")
    
    # 断言
    for assertion in case.get("assertions", []):
        lines.append(f"    assert response.{assertion['target']} {assertion['type']} {assertion['expected_value']}")
    
    return "\n".join(lines)
```

### 4.2 执行元数据注入

在 UTCF 的 `execution` 字段中，可以注入框架特定的执行配置：

```json
{
  "execution": {
    "timeout_seconds": 30,
    "retry_count": 2,
    "depends_on": ["TC-AUTH-0001-0001"],
    "skip_conditions": ["env == 'dev'"],
    "framework_specific": {
      "pytest": {
        "fixtures": ["login_client", "db_session"],
        "parametrize": {"username": ["user1", "user2", "user3"]}
      },
      "junit": {
        "groups": ["smoke", "auth"],
        "data_provider": "loginDataProvider"
      }
    }
  }
}
```

---

## 5. 闭环回流机制

### 5.1 写入知识库的 UTCF 增强

用例写入知识库时，UTCF 需要增加知识库特有字段：

```json
{
  "_kb_metadata": {
    "kb_version": "2.1",
    "ingested_at": "2024-06-25T10:00:00Z",
    "embedding_model": "bge-large-zh-v1.5",
    "embedding_version": "v1",
    "embedding_dim": 1024,
    "weight": 0.95,
    "decay_base_date": "2024-06-25",
    "half_life_days": 180,
    "feedback_history": [
      {"date": "2024-06-25", "reviewer": "zhangsan", "action": "approved", "comment": ""},
      {"date": "2024-07-15", "reviewer": "lisi", "action": "modified", "comment": "修正了步骤3的预期结果"}
    ],
    "execution_stats": {
      "total_runs": 15,
      "pass_count": 14,
      "fail_count": 1,
      "last_pass_rate": 0.933,
      "avg_duration_ms": 2340
    }
  }
}
```

### 5.2 回流流程图

```
GenAgent → UTCF JSON
              │
              ├──► FormatEngine → Gherkin/pytest/JUnit → CI/CD 执行
              │                                              │
              │                                        执行结果反馈
              │                                       (status / error_msg)
              │                                              │
              │                                ┌─────────────┴─────────────┐
              │                                │                           │
              │                       执行通过 / 代码 Bug              疑似用例错误
              │                                │                           │
              │                       直接更新 execution_stats      ReviewAgent.error_case_review
              │                                │                     (根因分类 4 类)
              │                                │                           │
              └──► KB Ingest Pipeline ─────────┘                           │
                       │                                                    │
                       ├── 提取文本 → Embedding → Vector DB                 │
                       ├── 提取元数据 → PostgreSQL                          │
                       ├── 更新权重、执行统计                                │
                       └── error_case: 写入 _kb_metadata.error_case ◄──────┘
                            ├── action=fix_and_version → 新版本 UTCF 入库
                            ├── action=obsolete → weight ×0.3
                            └── pattern_feedback → 反向更新测试模式库
```

**支持的反馈类型**：

| feedback_type | 触发条件 | 处理动作 |
|---------------|---------|---------|
| `execution_result` | CI/CD 执行完成 | 更新 `_kb_metadata.execution_stats` + 调整 weight |
| `human_review` | 人工审核完成 | 写入 `feedback_history` + 调整 weight |
| `error_review` | ReviewAgent 对执行失败用例复核完成 | 写入 `_kb_metadata.error_case` + 按 action 调整 weight / 生成新版本 / 标记 obsolete（详见 [doc-03 §7.2.2](./03-case-generation-flow.md#722-error_case-完整闭环优化项4)） |

---

## 6. 版本兼容策略

```python
# UTCF Schema 版本管理
SCHEMA_VERSIONS = {
    "1.0": {
        "fields": ["id", "title", "type", "priority", "steps", "preconditions", "test_data"],
        "migrate_from": None  # 初始版本
    },
    "1.1": {
        "fields": ["...", "assertions", "execution"],
        "migrate_from": {
            "1.0": lambda v1_case: {
                **v1_case,
                "assertions": _infer_assertions(v1_case["steps"]),
                "execution": {"timeout_seconds": 30}
            }
        }
    },
    "2.0": {
        "fields": ["...", "parameterized", "data_driven"],
        "migrate_from": {
            "1.1": lambda v1_case: _migrate_v1_to_v2(v1_case)
        }
    }
}
```

---

## 7. UTCF 用例生命周期

> 本节描述一条 UTCF 用例从生成到归档的完整状态流转，作为 §6 版本兼容策略（Schema 维度）
> 与 §8 覆盖率矩阵（追溯维度）之间的过渡衔接。

### 7.1 状态机

```
   ┌─────────────┐  审核通过   ┌─────────────┐  入库     ┌─────────────┐
   │   draft     │ ─────────▶ │  reviewed   │ ────────▶ │  approved   │
   │ (GenAgent)  │            │ (ReviewAgent)│           │ (知识库入库) │
   └─────┬───────┘            └──────┬───────┘           └──────┬───────┘
         │ reject                    │ reject                  │ 退役
         ▼                           ▼                         ▼
   ┌─────────────┐            ┌─────────────┐           ┌─────────────┐
   │  discarded  │            │  rejected   │           │  archived   │
   │ (直接丢弃)  │            │ (人工修正)   │           │ (历史保留)   │
   └─────────────┘            └─────────────┘           └─────────────┘
```

### 7.2 阶段与字段写入时机

| 阶段 | 触发节点 | 写入/更新的 UTCF 字段 |
|------|---------|---------------------|
| draft | GenAgent 生成完成 | `id, title, type, priority, steps, preconditions, test_data, assertions, gen_metadata, confidence(初值)` |
| reviewed | ReviewAgent 复核完成 | `review.reviewed_by, review.reviewed_at, review.issues, review.suggestions` |
| approved | 人工或自动通过 | `execution.last_run, status="approved"`，同步写入 KB `cases` 集合 |
| archived | 被新版用例取代 | `status="archived", superseded_by=<新用例ID>`，向量库保留但权重降为 0.2 |

### 7.3 与其他章节的衔接

- **生成阶段**：UTCF 字段定义见 [§2 通用测试用例格式](#2-通用测试用例格式-utcf)
- **审核阶段**：ReviewAgent 检查规则见 [doc-03 §5 ReviewAgent](./03-case-generation-flow.md#5-reviewagent)
- **入库阶段**：写入 KB 的字段映射见 [templates/knowledge-base-schema.json](../templates/knowledge-base-schema.json) `kb_test_cases.metadata_schema`
- **版本兼容**：Schema 升级时的字段迁移策略见 [§6 版本兼容策略](#6-版本兼容策略)
- **追溯关系**：状态流转中的 `req_id` / `superseded_by` 字段用于 [§8 追溯矩阵](#8-覆盖率矩阵与追溯矩阵) 构建

---

## 8. 覆盖率矩阵与追溯矩阵

### 8.1 覆盖率矩阵

每次生成用例后，输出覆盖率矩阵，展示需求→用例的覆盖关系：

```json {
  "coverage_matrix": {
    "requirement_id": "REQ-2024-001",
    "total_test_points": 6,
    "covered_test_points": 5,
    "coverage_rate": 0.83,
    "details": [
      {
        "test_point": "正常登录流程",
        "category": "functional",
        "covered_by": ["TC-AUTH-0001-0001"],
        "status": "covered"
      },
      {
        "test_point": "密码长度边界值测试",
        "category": "boundary",
        "covered_by": ["TC-AUTH-0001-0003"],
        "status": "covered"
      },
      {
        "test_point": "高并发登录压测",
        "category": "performance",
        "covered_by": [],
        "status": "uncovered",
        "suggestion": "需补充性能测试用例"
      }
    ]
  }
}
```

### 8.2 追溯矩阵输出

```json
{
  "traceability_matrix": {
    "requirement_id": "REQ-2024-001",
    "total_cases": 12,
    "traced_cases": 11,
    "orphan_cases": 1,
    "traceability_rate": 0.92,
    "links": [
      {
        "case_id": "TC-AUTH-0001-0001",
        "req_field": "flows.happy_path",
        "trace_type": "direct",
        "confidence": 1.0
      },
      {
        "case_id": "TC-AUTH-0001-0003",
        "req_field": "boundary_conditions",
        "trace_type": "direct",
        "confidence": 1.0
      },
      {
        "case_id": "TC-AUTH-0001-0012",
        "req_field": null,
        "trace_type": null,
        "confidence": null,
        "warning": "孤立用例: 无需求依据，建议人工审查"
      }
    ]
  }
}
```

### 8.3 变更影响分析

当需求变更时，通过追溯矩阵自动定位受影响的用例：

```json
{
  "impact_analysis": {
    "changed_requirement": "REQ-2024-001",
    "change_type": "modified",
    "affected_cases": ["TC-AUTH-0001-0001", "TC-AUTH-0001-0003"],
    "recommended_action": "re_generate",
    "details": "需求 flows.happy_path 变更，影响 2 个直接追溯用例"
  }
}
```

---

## 9. Markdown 输出格式（人类友好展示） ⭐ 新增

> **设计决策**：不是 JSON 和 Markdown 二选一，而是 **UTCF JSON 为主、Markdown 为辅** 的混合方案。

### 9.0 格式选型分析

#### 9.0.1 JSON vs Markdown 核心对比

| 维度 | UTCF JSON | Markdown |
|------|-------------|----------|
| 机器可读 | ⭐⭐⭐⭐⭐ 结构化，类型安全 | ⭐⭐ 需解析，易出错 |
| 人类可读 | ⭐⭐ 嵌套深，阅读门槛高 | ⭐⭐⭐⭐⭐ 纯文本，一目了然 |
| Agent 处理 | ⭐⭐⭐⭐⭐ 直接传递对象 | ⭐⭐ 需序列化/解析 |
| 数据库存储 | ⭐⭐⭐⭐⭐ PostgreSQL JSONB 原生支持 | ⭐⭐ TEXT 字段，查询低效 |
| Git diff | ⭐⭐⭐ 结构化 diff，精确但可读性一般 | ⭐⭐⭐⭐⭐ 语义 diff，Review 体验极佳 |
| 测试人员接受度 | ⭐ 需培训 | ⭐⭐⭐⭐⭐ 零门槛 |

**结论：两者互补，不可偏废。**

#### 9.0.2 真实测试团队工作流分析

**工作流 A：纯自动化团队（开发自测）**
```
开发 → 写代码 → AI 生成用例(JSON) → pytest 直接执行 → CI/CD
```
- JSON 全程够用，不需要 Markdown
- 适用：敏捷开发、DevOps 成熟团队

**工作流 B：测试/开发协作（最常见）**
```
ReqAgent → UTCF JSON → FormatEngine → Markdown
                                                              ↓
                                                    测试人员在 GitHub PR 中 Review
                                                              ↓
                                                    修改后 → markdown_to_utcf() → 回存 JSON
                                                              ↓
                                                        pytest / JUnit 执行
```
- **Markdown 是协作枢纽**，JSON 是执行底层
- 适用：大多数企业的测试团队

**工作流 C：手工测试为主**
```
AI 生成 Markdown 用例文档 → 测试人员按步骤手工执行 → Excel 记录结果
```
- Markdown 直接作为测试说明书，不需要 JSON
- 适用：传统测试团队，手工测试占比高

**工作流 D：监管合规行业（金融/医疗）**
```
AI 生成 → Markdown(人工 Review) → 签字确认 → JSON(归档) → 测试执行 → 报告
```
- 需要 **Markdown(评审) + JSON(归档)** 双格式
- 适用：需要审计追溯的行业

#### 9.0.3 混合方案设计

```
                    UTCF JSON (系统内部格式)
                             │
            ┌────────────────┼────────────────┐
            ▼                ▼                ▼
       Agent 处理        FormatEngine     知识库存储
       (LangGraph)     (输出转换层)      (PostgreSQL)
            │                │                │
            └────────────────┼────────────────┘
                             │
            ┌────────────────┼────────────────┐
            ▼                ▼                ▼
       Gherkin          pytest/JUnit     Markdown (Review)
       (Cucumber)      (自动化执行)    (GitHub PR)
```

**FormatEngine 输出矩阵**：

| 目标受众 | 输出格式 | 转换函数 |
|---------|---------|-----------|
| LangGraph Agent | UTCF JSON | _（原生格式）_ |
| 测试人员 Review | Markdown | `utcf_to_markdown()` |
| Cucumber 执行 | Gherkin `.feature` | `utcf_to_gherkin()` |
| pytest 执行 | `.py` 测试文件 | `utcf_to_pytest()` |
| JUnit 执行 | `.java` 测试文件 | `utcf_to_junit()` |
| 知识库存储 | UTCF JSON | _（直接写入）_ |
| 审计/归档 | Markdown + JSON | 双格式同时输出 |

#### 9.0.4 决策规则（一句话版）

| 场景 | 用啥格式 | 原因 |
|------|---------|------|
| Agent 处理、存数据库、API | **JSON (UTCF)** | 结构化，类型安全 |
| 测试人员 Review、Git 管理 | **Markdown** | 可读，diff 友好 |
| 自动化执行 | **pytest / JUnit / Gherkin** | 框架原生支持 |

---

### 9.1 设计目标

| 目标 | 说明 |
|------|------|
| **人类可读** | 测试人员一眼就能看懂用例内容 |
| **Git 友好** | 支持 Git diff，清晰展示变更 |
| **Review 友好** | 支持 GitHub/GitLab PR Review |
| **编辑友好** | 任何文本编辑器都能修改 |
| **可追溯** | 保留需求追溯信息 |

### 9.2 UTCF → Markdown 转换规则

#### 9.2.1 单个用例的 Markdown 格式

```markdown
## TC-AUTH-0001-0001: 正常登录流程验证

**类型**: 功能测试 | **优先级**: P0 | **标签**: `smoke` `regression`  
**追溯**: [REQ-2024-001](./requirements/REQ-2024-001.md) | **置信度**: 95%  
**生成模型**: deepseek-v4 | **参考模式**: auth_pattern

---

### 前置条件

- [ ] 测试账号 `test_user` 已在系统中注册
- [ ] 账号未处于锁定状态
- [ ] 数据库连接正常

---

### 测试步骤

| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 打开登录页面 | 页面正常加载，显示用户名、密码输入框和登录按钮 |
| 2 | 输入用户名 `test_user` | 输入框显示输入的用户名 |
| 3 | 输入密码 `Test@123456` | 密码以密文形式显示 |
| 4 | 点击登录按钮 | 登录成功，页面跳转到首页，顶部显示用户名 |

---

### 测试数据

```json
{
  "username": "test_user",
  "password": "Test@123456"
}
```

---

### 断言

- [ ] `response.status_code` = `200`
- [ ] `response.token` 不为空
- [ ] `response.user_id` 等于预期值

---

### 执行配置

- **超时**: 15 秒
- **重试次数**: 0
- **依赖用例**: 无

---

### 清理操作

- [ ] 退出登录
- [ ] 清理 Session

---

**AI 生成备注**: 此用例由 AI 生成，置信度 95%。如有问题，请在 Review 中标注。
```

#### 9.2.2 批次用例文档（多个用例）

```markdown
# 测试用例文档

**需求**: REQ-2024-001 用户登录功能  
**生成时间**: 2024-06-24 16:30:00  
**生成模型**: deepseek-v4  
**批次 ID**: batch-20240624-001

---

## 覆盖率概览

| 类型 | 数量 |
|------|------|
| 功能测试 | 4 |
| 边界值测试 | 3 |
| 异常测试 | 2 |
| 安全测试 | 2 |
| 性能测试 | 1 |
| **总计** | **12** |

---

## 功能测试用例

### TC-AUTH-0001-0001: 正常登录流程验证

**优先级**: P0 | **标签**: `smoke` `regression`

**前置条件:**
- 测试账号已在系统中注册
- 账号未处于锁定状态

**测试步骤:**

| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 打开登录页面 | 页面正常加载，显示登录表单 |
| 2 | 输入正确的用户名和密码 | 输入框正常显示 |
| 3 | 点击登录按钮 | 登录成功，跳转首页 |

---

### TC-AUTH-0001-0002: 密码错误三次后验证码出现

**优先级**: P1

**测试步骤:**

| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 连续 3 次输入错误密码 | 第 3 次后出现验证码 |
| 2 | 输入正确密码 + 正确验证码 | 登录成功 |

---

## 边界值测试用例

### TC-AUTH-0001-0003: 密码长度边界值测试

**优先级**: P2

**测试步骤:**

| 步骤 | 操作 | 预期结果 |
|------|------|---------|
| 1 | 输入 5 位密码 | 提示「密码至少6位」 |
| 2 | 输入 6 位密码 | 允许提交 |
| 3 | 输入 128 位密码 | 允许提交 |
| 4 | 输入 129 位密码 | 提示「密码过长」 |

---

## 追溯矩阵

| 用例 ID | 需求字段 | 追溯类型 | 置信度 |
|---------|---------|---------|--------|
| TC-AUTH-0001-0001 | flows.happy_path | direct | 100% |
| TC-AUTH-0001-0003 | boundary_conditions | direct | 100% |
| TC-AUTH-0001-0008 | — | inferred | 70% |

---

**文档生成时间**: 2024-06-24 16:35:00  
**下一步**: 请在 GitLab PR 中 Review 此文档，修改后保存。
```

### 9.3 Python 转换器实现

```python
# format_engines/markdown_converter.py

def utcf_to_markdown(utcf: dict) -> str:
    """
    将 UTCF JSON 转换为 Markdown 文档
    
    Args:
        utcf: UTCF JSON 对象
        
    Returns:
        Markdown 格式的测试用例文档
    """
    lines = []
    
    # 文档标题
    req = utcf["metadata"]["source_requirement"]
    lines.append(f"# 测试用例文档\n")
    lines.append(f"**需求**: {req['id']} {req['title']}  ")
    lines.append(f"**生成时间**: {utcf['metadata']['generated_at']}  ")
    lines.append(f"**生成模型**: {utcf['metadata']['generator_model']}  ")
    lines.append(f"**批次 ID**: {utcf['metadata']['gen_batch_id']}\n")
    lines.append("---\n")
    
    # 覆盖率概览
    coverage = utcf["metadata"]["coverage_summary"]
    lines.append("## 覆盖率概览\n")
    lines.append("| 类型 | 数量 |")
    lines.append("|------|------|")
    for typ, count in coverage.items():
        lines.append(f"| {typ} | {count} |")
    lines.append(f"| **总计** | **{utcf['metadata']['total_cases']}** |\n")
    lines.append("---\n")
    
    # 按类型分组输出用例
    cases_by_type = {}
    for case in utcf["test_suite"]:
        typ = case["type"]
        if typ not in cases_by_type:
            cases_by_type[typ] = []
        cases_by_type[typ].append(case)
    
    for typ, cases in cases_by_type.items():
        lines.append(f"## {typ}测试用例\n")
        
        for case in cases:
            lines.append(f"### {case['id']}: {case['title']}\n")
            lines.append(f"**优先级**: {case['priority']} | **标签**: {' '.join(['`' + t + '`' for t in case.get('tags', [])])}  ")
            lines.append(f"**追溯**: {case.get('related_requirement', 'N/A')}  ")
            lines.append(f"**置信度**: {int(case.get('confidence', 0) * 100)}%\n")
            
            # 前置条件
            if case.get("preconditions"):
                lines.append("**前置条件:**\n")
                for pc in case["preconditions"]:
                    lines.append(f"- [ ] {pc}")
                lines.append("")
            
            # 测试步骤
            lines.append("**测试步骤:**\n")
            lines.append("| 步骤 | 操作 | 预期结果 |")
            lines.append("|------|------|---------|")
            for step in case["steps"]:
                lines.append(f"| {step['step']} | {step['action']} | {step['expected']} |")
            lines.append("")
            
            # 断言
            if case.get("assertions"):
                lines.append("**断言:**\n")
                for assertion in case["assertions"]:
                    lines.append(f"- [ ] `{assertion['target']}` {assertion['type']} `{assertion['expected_value']}`")
                lines.append("")
            
            lines.append("---\n")
    
    return "\n".join(lines)


def markdown_to_utcf(markdown_text: str) -> dict:
    """
    将 Markdown 用例文档转回 UTCF JSON（支持人工修改后的回存）
    
    Args:
        markdown_text: Markdown 格式的用例文档
        
    Returns:
        UTCF JSON 对象
    """
    # 使用正则表达式解析 Markdown 结构
    # 1. 提取用例 ID、标题、优先级、标签
    # 2. 提取步骤表格
    # 3. 提取断言列表
    # 4. 组装为 UTCF JSON
    
    import re
    
    utcf = {
        "format_version": "1.0",
        "metadata": {
            "generated_at": "...",  # 从 Markdown 提取或重新生成
            "generator": "ai-test-gen (markdown-import)",
            "source_requirement": {"id": "...", "title": "...", "version": 1},
            "total_cases": 0,
            "coverage_summary": {}
        },
        "test_suite": []
    }
    
    # 按 ### 分割用例
    case_sections = re.split(r'\n### ', markdown_text)
    
    for section in case_sections[1:]:  # 跳过第一个（文档标题部分）
        case = _parse_markdown_case_section(section)
        utcf["test_suite"].append(case)
    
    utcf["metadata"]["total_cases"] = len(utcf["test_suite"])
    
    return utcf


def _parse_markdown_case_section(section: str) -> dict:
    """解析单个用例的 Markdown 章节"""
    import re
    
    case = {}
    
    # 提取 ID 和标题
    header_match = re.match(r'(TC-[A-Z]+-\d{4}-\d{4}): (.+)\n', section)
    if header_match:
        case["id"] = header_match.group(1)
        case["title"] = header_match.group(2)
    
    # 提取优先级
    priority_match = re.search(r'\*\*优先级\*\*: (P[0-3])', section)
    if priority_match:
        case["priority"] = priority_match.group(1)
    
    # 提取类型
    type_match = re.search(r'\*\*类型\*\*: (.+?) \|', section)
    if type_match:
        case["type"] = type_match.group(1).strip()
    
    # 提取标签
    tags_match = re.search(r'\*\*标签\*\*: (.+?)\n', section)
    if tags_match:
        tags_str = tags_match.group(1)
        case["tags"] = re.findall(r'`([^`]+)`', tags_str)
    
    # 提取前置条件
    preconditions = re.findall(r'- \[ \] (.+)', section)
    if preconditions:
        case["preconditions"] = preconditions
    
    # 提取步骤表格
    steps = []
    table_rows = re.findall(r'\| (\d+) \| (.+?) \| (.+?) \|', section)
    for row in table_rows:
        steps.append({
            "step": int(row[0]),
            "action": row[1].strip(),
            "expected": row[2].strip()
        })
    if steps:
        case["steps"] = steps
    
    # 提取断言
    assertions = []
    assertion_lines = re.findall(r'- \[ \] `(.+?)` (.+?) `(.+?)`', section)
    for al in assertion_lines:
        assertions.append({
            "target": al[0],
            "type": al[1].strip(),
            "expected_value": al[2]
        })
    if assertions:
        case["assertions"] = assertions
    
    return case
```

### 9.4 使用场景

| 场景 | 格式 | 说明 |
|------|------|------|
| **AI 生成后 Review** | Markdown | 测试人员在 GitHub/GitLab PR 中 Review |
| **批量审批** | Markdown | 导出为 PDF，线下评审会使用 |
| **需求变更影响分析** | Markdown | 展示变更前后的用例差异（Git diff） |
| **测试报告** | Markdown / HTML | 展示测试结果和覆盖率 |
| **知识库检索结果展示** | Markdown | 检索到的相似用例以 Markdown 展示 |

---

## 10. 业界借鉴

| 实践来源 | 借鉴点 |
|----------|--------|
| **Cucumber/Gherkin** | Given-When-Then 结构、Feature/Scenario 层级 |
| **JUnit 5** | 注解驱动、参数化测试、动态测试 |
| **pytest** | fixture 机制、parametrize 数据驱动 |
| **Allure Report** | 测试报告元数据（标签、步骤、附件） |
| **OpenAPI/Swagger** | API 测试用例从接口定义自动推导 |
| **Postman Collections** | 集合格式 → 可执行的 HTTP 测试 |

---

## 11. 实施建议

1. **UTCF 作为唯一内部格式**，所有生成/存储/检索都基于 UTCF，只在输出边界做转换
2. **优先实现 UTCF → Gherkin 和 UTCF → pytest 两条转换路径**，覆盖 BDD 和单元测试两大场景
3. **Schema 版本化 + 自动迁移**，避免格式升级导致历史数据不可用
4. **UTCF 中 confidence 字段必须保留**，作为后续人工审核优先级排序的依据
5. **定期清理低权重历史用例**（weight < 0.2 且超过 1 年未执行），控制知识库膨胀

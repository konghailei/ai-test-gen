# 08 功能点（FunctionalPoint）子系统

## 概述

功能点（FP）是 ai-test-gen v1.5.1 的核心子系统。它采用 **「原始文档 → FP 一体式提取 → 子 SRR 生成 → 审核 → 用例生成」** 的流水线，将传统的大规模 SRR 拆分为按业务能力划分的功能点，每个 FP 附带完整的子 SRR。

## 架构层级

```
Product（产品）
  └── Module（模块，如"用户管理"）
        └── FunctionalPoint（功能点，如"登录""注册""角色管理"）
              └── sub_srr（子 SRR：flows / business_rules / constraints / acceptance_criteria）
                    └── TestCase（测试用例，由 gen_agent 按 FP 定向生成）
```

## FP 生命周期状态机

```
┌──────────────┐     审核通过     ┌──────────┐
│ pending_review│ ────────────→   │ approved │
│ （默认初始状态）│                 │ （可生成用例）│
└──────────────┘                 └──────────┘
       │                               │
       │ AI 重生成（regenerate）        │ 归档
       ↓                               ↓
┌──────────────┐                 ┌──────────┐
│ pending_review│                 │ archived │
│ （需重新审核） │                 │ （只读）  │
└──────────────┘                 └──────────┘
```

## 核心流程

### 1. FP 一体式提取（ReqAgent._fp_extract_from_raw）

**时机**：ReqAgent.parse() 完成 SRR 解析后，自动触发。

**输入**：
- 原始需求文档（最多 96000 字符）
- 需求级概要 SRR

**输出**：
- `functional_points[]`：功能点列表，每个含 name / fp_type / sub_srr / test_hints
- `dependencies[]`：功能点间依赖关系（5 种类型）

**Prompt 策略**：
- 粒度：业务能力级（"登录"是 1 个 FP，密码校验、失败锁定属于其 sub_srr）
- 宁缺勿滥：单功能需求 1-2 个 FP，复杂需求 6-12 个 FP

### 2. FP 审核（fp_approve）

- 每个 FP 初始状态为 `pending_review`
- 用户在「功能点 SRR」Tab 中审核通过 → `status = "approved"`
- 所有 FP 审核通过 → 解锁测试用例生成

### 3. FP AI 重生成（fp_regenerate）

- 用户可输入补充说明（hint）引导 LLM 重新生成子 SRR
- 重生成后 FP 状态回退为 `pending_review`（需重新审核）

### 4. FP 依赖图谱

- 上传时自动提取依赖关系，存入 `fp_dependencies` 表
- 支持跨需求依赖
- 依赖关系类型：
  - `depends_on`：A 的前置条件是 B
  - `data_flows`：A 的输出数据是 B 的输入
  - `invariants`：A 和 B 共享同一全局约束
  - `lifecycle_scenarios`：属于同一端到端流程
  - `conflicts_with`：互斥

### 5. FP 影响分析

基于 FPDependency BFS 遍历的变更影响传播：
- `depends_on` 边：修改被依赖方影响依赖方（反向传播）
- `data_flows` / `lifecycle_scenarios` 边：修改来源方影响目标方（正向传播）
- BFS 深度限制：默认 3 层
- 返回受影响的功能点 + 聚合模块列表 + 影响摘要

## API 端点

| 方法 | 路径 | 功能 | 权限 |
|------|------|------|------|
| GET | `/{req_id}/fp` | 功能点列表 | 登录用户 |
| PATCH | `/{req_id}/fp/{fp_id}` | 编辑 FP | 登录用户 |
| DELETE | `/{req_id}/fp/{fp_id}` | 删除 FP | 管理员 |
| PATCH | `/{req_id}/fp/{fp_id}/approve` | 审核通过 | 写权限 |
| PATCH | `/{req_id}/fp/{fp_id}/regenerate` | AI 重生成 | 写权限 |
| GET | `/{req_id}/fp/dependencies` | 依赖关系 | 登录用户 |
| GET | `/{product_id}/fp/dependency-graph` | 跨需求依赖图谱 | 登录用户 |
| GET | `/{product_id}/fp/impact?fp_id=X` | 影响分析 | 登录用户 |

## 前端 UI

- **需求详情 → 「功能点 SRR」Tab**：按模块分组展示 FP + 子 SRR 展开 + 审核 + 重生成 + 影响分析
- **需求详情 → 「功能点」Tab**：精简 FP 列表（编辑名称/模块/删除/重新拆分）
- **依赖图谱页面**：跨需求 FP 依赖可视化

## DB 模型

### functional_points

| 字段 | 类型 | 说明 |
|------|------|------|
| id | VARCHAR(64) PK | FP 唯一标识 |
| requirement_id | FK → requirements.id | 所属需求（CASCADE）|
| module_id | FK → modules.id | 归属模块（SET NULL）|
| name | VARCHAR(255) | 功能点名称 |
| fp_type | VARCHAR(32) | workflow/constraint/calculation/api_behavior/data_record/config |
| status | VARCHAR(16) | pending_review/approved/archived |
| srr_json | JSON | 完整子 SRR |
| atomicity_check | JSON | 三要素自检 |
| test_hints | JSON[] | 测试提示 |

### fp_dependencies

| 字段 | 类型 | 说明 |
|------|------|------|
| source_fp_id | FK → functional_points.id | 源 FP（CASCADE）|
| target_fp_id | FK → functional_points.id | 目标 FP（CASCADE）|
| relation_type | VARCHAR(32) | depends_on/data_flows/invariants/lifecycle_scenarios/conflicts_with |
| description | TEXT | 关系描述 |

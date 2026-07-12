# 澄清历史标签页 + 需求重新生成功能

## Context

当前需求澄清流程存在两个限制：
1. 澄清提交后（status=active），澄清问题和回答丢失——`submit_clarify_answers` 会删除 `_clarify_state`，用户无法回顾之前问了什么、答了什么。
2. 澄清完成后无法再补充信息重新生成 SRR——用户如果发现遗漏，只能手动编辑 SRR JSON，无法走 ReqAgent 重新解析。

用户需求：
- 人工澄清也要有一个标签页存放 Q&A 历史
- 澄清后仍可在澄清页面输入信息重新生成需求分析
- 重新生成操作需记录下来

## 方案

### 1. 后端：存储澄清历史 + 重新生成端点

**文件：`backend/app/api/requirements.py`**

a) **修改 `submit_clarify_answers`（POST /{req_id}/clarify）**
- 在 re-parse 之前，将当前 Q&A 快照追加到 `srr_json._clarify_history` 数组
- 每条记录：`{timestamp, action: "clarify", questions, answers, extra_notes, auto_augmentations, field_conflicts, quality_report}`
- re-parse 后的新 SRR 保留 `_clarify_history`（不随 `_clarify_state` 一起删除）

b) **新增 `POST /{req_id}/regenerate`**
- 请求体：`{extra_notes: string}`（用户补充说明）
- 流程：
  1. 读取当前 raw_text + srr_json
  2. 将 extra_notes 追加到 enhanced_raw（raw_text + 已有 KB 补全 + extra_notes）
  3. 调用 `req_agent.parse()` 重新生成 SRR
  4. 将操作记录追加到 `_clarify_history`：`{timestamp, action: "regenerate", extra_notes, previous_title, new_title}`
  5. 更新 DB，返回新 SRR
- 允许 status=active 时调用（核心需求）

c) **新增 `GET /{req_id}/clarify/history`**
- 返回 `srr_json._clarify_history` 数组（按时间倒序）

**文件：`backend/app/models/api_schemas.py`**
- 新增 `RegenerateRequest(BaseModel)`: `extra_notes: str`

### 2. 前端：澄清历史 Tab + 重新生成入口

**文件：`frontend/src/types/index.ts`**
- 新增 `ClarifyHistoryEntry` 接口：
  ```ts
  interface ClarifyHistoryEntry {
    timestamp: string
    action: 'clarify' | 'regenerate'
    questions?: ClarifyQuestion[]
    answers?: Record<string, string>
    extra_notes?: string
    auto_augmentations?: KbAugmentation[]
    field_conflicts?: FieldConflict[]
    quality_report?: { overall_score: number; summary?: string }
    previous_title?: string
    new_title?: string
  }
  ```

**文件：`frontend/src/api/client.ts`**
- 新增 `regenerate(id, extraNotes)` → POST /{req_id}/regenerate
- 新增 `getClarifyHistory(id)` → GET /{req_id}/clarify/history

**文件：`frontend/src/pages/RequirementDetail.tsx`**
- 在 Tab 导航栏新增"澄清历史"Tab（与 srr/raw/kb/impact 并列）
- Tab 内容：时间线展示 `_clarify_history`，每条记录显示：
  - action 类型（澄清 / 重新生成）
  - 时间戳
  - 澄清：Q&A 列表 + 补充说明 + 质量报告
  - 重新生成：补充说明 + 标题变化
- Tab 标签上显示计数 badge（如有历史记录）

**文件：`frontend/src/pages/RequirementClarify.tsx`**
- 当需求 status=active（已澄清）时，不显示"done"页，改为显示：
  - "需求已澄清"状态提示
  - "重新生成需求分析"区域：textarea 输入框 + "重新生成"按钮
  - 历史记录摘要（最近几条操作）
- 重新生成成功后，刷新页面显示新 SRR 摘要
- status=draft 时保持现有行为（显示澄清问题）

**文件：`frontend/src/pages/RequirementDetail.tsx`（按钮区域）**
- status=active 时增加"补充重新生成"按钮，跳转到 `/requirements/{id}/clarify`

### 3. 验证

1. 上传第一个文档（完整）→ status=active → 检查澄清历史 Tab 为空或无记录
2. 上传第二个文档（增量）→ 如有澄清问题 → 提交答案 → 检查澄清历史 Tab 显示一条 "clarify" 记录
3. 从澄清页面输入补充信息 → 点击重新生成 → 检查：
   - SRR 已更新
   - 澄清历史 Tab 新增一条 "regenerate" 记录
4. 运行 `test_data/test_e2e_full.py` 确认全链路未回归

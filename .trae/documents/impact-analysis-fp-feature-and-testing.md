# 影响功能说明特性 + 系统全方位测试计划

> **任务来源**：用户 `/plan` 指令
> **核心目标**：
> 1. 拉取最新代码并审查潜在漏洞（已完成审查）
> 2. 新增「影响功能说明」特性：在依赖图谱中点击被依赖的模块/功能点 → 自动生成影响说明 → 写入需求 SRR 新字段 → 服务后续测试用例生成
> 3. 全方位系统测试：第一轮（需求上传→分析→归档全链路）、第二轮（优化需求上传→知识库查询→FP 影响范围分析）

---

## 一、当前状态分析（Phase 1 探索结论）

### 1.1 现有依赖图谱与影响分析架构

| 组件 | 现状 | 缺口 |
|---|---|---|
| `ImpactAnalysisAgent` ([backend/app/agents/impact_agent.py](file:///e:/git-down/ai-test-gen/backend/app/agents/impact_agent.py#L35-L320)) | 仅遍历 `ModuleDependency` 图（BFS ≤3 跳），需 `optimization_context` 才触发 | **不遍历 `FPDependency`**；无独立 FP 影响分析入口 |
| `GET /{req_id}/impact` ([backend/app/api/requirements.py:1914](file:///e:/git-down/ai-test-gen/backend/app/api/requirements.py#L1914)) | 直接调用 `_analyze_module_impact()`，仅返回模块维度 | 没有 FP 维度影响分析端点 |
| `GET /{product_id}/fp/dependency-graph` ([backend/app/api/products.py:502](file:///e:/git-down/ai-test-gen/backend/app/api/products.py#L502)) | 返回跨需求 FP 节点+边 | **前端无点击节点触发影响说明的交互** |
| `DependencyGraph.tsx` ([frontend/src/pages/DependencyGraph.tsx:424](file:///e:/git-down/ai-test-gen/frontend/src/pages/DependencyGraph.tsx#L424)) | `onNodeDoubleClick` 在 FP 模式直接 `return` | FP 节点无可点击的"生成影响说明"入口 |
| SRR Schema ([backend/app/prompts/req_agent.py:82](file:///e:/git-down/ai-test-gen/backend/app/prompts/req_agent.py#L82)) | 字段含 `_is_incremental` / `_baseline_gaps` 等 | **无 `impact_analysis` 字段** |
| `gen_agent.build_user_prompt()` ([backend/app/prompts/gen_agent.py:108](file:///e:/git-down/ai-test-gen/backend/app/prompts/gen_agent.py#L108)) | 注入 `impact_section`（来自 ImpactAgent）和 `fp_section` | **不读 SRR `impact_analysis` 字段** |

### 1.2 潜在漏洞与代码质量问题

通过 `Grep` 搜索 `except Exception:\s*\n\s*pass` 共发现 **68 处** 静默吞异常：

| 文件 | 行号 | 严重程度 | 处理策略 |
|---|---|---|---|
| [backend/app/db/milvus_store.py:256](file:///e:/git-down/ai-test-gen/backend/app/db/milvus_store.py#L256), :371, :375 | 高 | Milvus 创建分区/读取 metadata 静默失败 → 向量检索可能丢数据 | **修复：改为 `logger.warning`** |
| [backend/app/api/knowledge.py:559](file:///e:/git-down/ai-test-gen/backend/app/api/knowledge.py#L559) | 中 | KB 操作静默失败 | **修复：改为 `logger.warning`** |
| [backend/app/api/requirements.py:673](file:///e:/git-down/ai-test-gen/backend/app/api/requirements.py#L673) | 中 | 需求归档相关清理静默失败 | **修复：改为 `logger.warning`** |
| [backend/app/agents/clarify_agent.py](file:///e:/git-down/ai-test-gen/backend/app/agents/clarify_agent.py#L871) L871/1232/1369/1390/1409/1429/1441/1453 | 低 | ClarifyAgent 内多处上下文构建辅助逻辑，失败可降级 | **保持现状（非关键路径）** |
| [backend/app/utils/crypto.py:119](file:///e:/git-down/ai-test-gen/backend/app/utils/crypto.py#L119) | 无 | Windows 不支持 chmod，已注释说明 | **保持现状** |

> **结论**：高/中级 5 处需修复，低级 9 处保持现状（避免过度工程化）。

---

## 二、设计方案（基于用户澄清）

### 2.1 用户澄清结论
- **生成方式**：自动生成 + 可编辑（用户可在自动生成后修改文本）
- **存储位置**：需求 SRR 新增字段 `impact_analysis`（数组结构，支持多 FP）

### 2.2 SRR `impact_analysis` 字段结构

```json
"impact_analysis": [
  {
    "fp_id": "FP-xxx",
    "fp_name": "功能点名称",
    "fp_type": "workflow",
    "module_id": "auth",
    "affected_fps": [
      {"fp_id": "FP-yyy", "fp_name": "依赖功能点", "relation_type": "depends_on", "req_id": "REQ-001", "req_title": "登录需求"}
    ],
    "affected_modules": [
      {"module_id": "session", "module_name": "会话管理", "reason": "直接依赖", "depth": 1}
    ],
    "affected_requirements": [
      {"req_id": "REQ-002", "req_title": "权限校验", "matched_modules": ["session"]}
    ],
    "description": "本次变更会影响：1) 功能点「依赖功能点」(depends_on) ...; 2) 模块「会话管理」...",
    "test_focus": "重点回归：依赖功能点的 happy_path；会话管理模块的登录态校验",
    "generated_at": "2026-07-15T10:00:00",
    "is_auto_generated": true
  }
]
```

### 2.3 后端：新增 FP 影响分析方法 + 端点

**`ImpactAnalysisAgent` 新增 `_analyze_fp_impact()` 方法**：
- 输入：`fp_id`, `session`
- 步骤：
  1. 反向 BFS `FPDependency`（找谁依赖此 FP，≤3 跳）
  2. 取 FP 所属 `module_id`，调用现有 `_analyze_module_impact()` 找受影响模块
  3. 用受影响模块匹配同产品其他需求（复用 `_find_regression_requirements` 逻辑）
  4. 用 LLM 把结构化数据合成自然语言 `description` + `test_focus`（自动生成）
- 输出：上述 JSON 结构的 dict（不含数组外壳）

**新增端点 `POST /requirements/{req_id}/fp/{fp_id}/impact-description`**：
- 权限：`require_write`
- 流程：调用 `_analyze_fp_impact()` → 读需求 SRR → 追加到 `srr_json["impact_analysis"]` 数组（同 fp_id 覆盖旧条目）→ 写回 DB → 返回生成结果
- 失败时不写 DB，返回 500 + 错误详情

**新增端点 `PUT /requirements/{req_id}/impact-description/{fp_id}`**：
- 权限：`require_write`
- 用途：用户编辑后保存（接收 `description` / `test_focus` 字段）
- 流程：定位 SRR `impact_analysis` 数组中匹配 fp_id 的条目 → 更新字段 → 写回 DB

### 2.4 前端：DependencyGraph FP 节点点击交互

**修改 `DependencyGraph.tsx`**：
1. `onNodeDoubleClick` 在 `graphType === 'fp'` 时不再 `return`，改为打开「影响说明」Modal
2. 新增 `ImpactDescriptionModal` 组件：
   - 展示：被依赖的 FP 列表、受影响模块列表、受影响需求列表
   - 「生成影响说明」按钮 → 调用 `POST /requirements/{req_id}/fp/{fp_id}/impact-description`
   - 生成后：`description` / `test_focus` 用 `<textarea>` 可编辑
   - 「保存」按钮 → 调用 `PUT /requirements/{req_id}/impact-description/{fp_id}`
3. 新增 client.ts 方法：
   - `requirements.generateImpactDescription(reqId, fpId)`
   - `requirements.saveImpactDescription(reqId, fpId, payload)`

### 2.5 GenAgent Prompt 增强

修改 [backend/app/prompts/gen_agent.py:108](file:///e:/git-down/ai-test-gen/backend/app/prompts/gen_agent.py#L108) `build_user_prompt()`：
- 新增 `impact_analysis_section`：从 `srr.get("impact_analysis", [])` 读取，注入 LLM prompt
- 内容包含每个 FP 的影响说明 + test_focus，指导 LLM 在生成用例时覆盖回归场景

---

## 三、实施步骤（按依赖顺序）

### 步骤 1：修复静默吞异常漏洞（高/中级）
- **文件**：
  - [backend/app/db/milvus_store.py](file:///e:/git-down/ai-test-gen/backend/app/db/milvus_store.py) L256/371/375 → 改 `pass` 为 `logger.warning(...)`
  - [backend/app/api/knowledge.py](file:///e:/git-down/ai-test-gen/backend/app/api/knowledge.py) L559 → 改 `pass` 为 `logger.warning(...)`
  - [backend/app/api/requirements.py](file:///e:/git-down/ai-test-gen/backend/app/api/requirements.py) L673 → 改 `pass` 为 `logger.warning(...)`
- **验证**：`docker compose restart backend` 无报错，日志能看到原 warning

### 步骤 2：SRR Schema 增加 `impact_analysis` 字段
- **文件**：[backend/app/prompts/req_agent.py](file:///e:/git-down/ai-test-gen/backend/app/prompts/req_agent.py) `SRR_SCHEMA_GUIDE` (L82-161)
- **改动**：在 `_baseline_gaps` 后追加 `impact_analysis` 字段说明（数组，可空）
- **注意**：字段标记为"可选，默认空数组"，不影响现有解析流程

### 步骤 3：ImpactAnalysisAgent 增加 FP 影响分析
- **文件**：[backend/app/agents/impact_agent.py](file:///e:/git-down/ai-test-gen/backend/app/agents/impact_agent.py)
- **新增方法**：
  - `_analyze_fp_impact(fp_id, session) -> dict`：反向 BFS FPDependency + 复用模块分析
  - `_build_fp_impact_description(fp, affected_fps, affected_modules, affected_reqs) -> tuple[str, str]`：LLM 合成自然语言描述 + 测试关注点
- **复用**：`_analyze_module_impact()`（已存在）、`_find_regression_requirements()` 逻辑

### 步骤 4：新增后端端点
- **文件**：[backend/app/api/requirements.py](file:///e:/git-down/ai-test-gen/backend/app/api/requirements.py)
- **位置**：在现有 FP 依赖端点后（L2366 之后）
- **新增**：
  - `POST /{req_id}/fp/{fp_id}/impact-description`（自动生成 + 写入 SRR）
  - `PUT /{req_id}/impact-description/{fp_id}`（用户编辑后保存）
- **Pydantic 模型**：`ImpactDescriptionUpdateRequest { description: str, test_focus: str }`

### 步骤 5：GenAgent Prompt 注入 impact_analysis
- **文件**：[backend/app/prompts/gen_agent.py](file:///e:/git-down/ai-test-gen/backend/app/prompts/gen_agent.py) `build_user_prompt()` (L108+)
- **改动**：读取 `srr.get("impact_analysis", [])`，非空时构建 `impact_analysis_section` 注入 prompt
- **内容**：列出每个 FP 的影响说明 + test_focus，指导 LLM 补充回归用例

### 步骤 6：前端 client.ts 新增 API 方法
- **文件**：[frontend/src/api/client.ts](file:///e:/git-down/ai-test-gen/frontend/src/api/client.ts)
- **新增方法**（在 `requirements` 对象内）：
  - `generateImpactDescription(reqId, fpId): Promise<ImpactDescription>`
  - `saveImpactDescription(reqId, fpId, payload): Promise<void>`

### 步骤 7：前端 DependencyGraph FP 节点点击交互
- **文件**：[frontend/src/pages/DependencyGraph.tsx](file:///e:/git-down/ai-test-gen/frontend/src/pages/DependencyGraph.tsx)
- **改动**：
  - `onNodeDoubleClick` FP 模式改为打开 `ImpactDescriptionModal`（不再 return）
  - 新增 `ImpactDescriptionModal` 组件（含生成按钮、可编辑 textarea、保存按钮）
  - 从节点 `data.reqId` + `data.id`（fp_id）构造请求参数

### 步骤 8：构建验证
- `docker compose build backend frontend`
- TypeScript 编译通过、Python 无语法错误

### 步骤 9：第一轮系统测试（需求上传 → 归档全链路）
1. 登录系统（admin/888888）
2. 上传完整需求文档（.md 格式）→ 验证 SRR 生成、Clarify 问题
3. 提交澄清答案 → 验证 SRR finalize
4. 拆分功能点 → 验证 FP 列表
5. 添加 FP 依赖 + 模块依赖
6. 生成测试用例（SSE 流式）→ 验证 FP 覆盖
7. 归档需求 → 验证知识库向量化（Milvus + PostgreSQL）
8. 知识库管理页查询 → 验证双状态徽章

### 步骤 10：第二轮系统测试（优化需求 → KB 查询 → FP 影响分析）
1. 上传优化需求（含 `optimization_context`）→ 验证 KB 检索增强
2. 在依赖图谱切换到 FP 模式 → 双击被依赖的 FP 节点
3. 点击「生成影响说明」→ 验证：
   - 受影响 FP 列表正确（反向 BFS）
   - 受影响模块列表正确（复用模块 BFS）
   - 受影响需求列表正确
   - description + test_focus 文本质量
4. 编辑 description → 保存 → 重新打开验证持久化
5. 生成测试用例 → 验证 prompt 中含 impact_analysis 内容
6. 验证用例 `related_fp` 字段追溯正确

---

## 四、假设与决策

| 编号 | 决策 | 理由 |
|---|---|---|
| D1 | `impact_analysis` 为 SRR 数组字段（非单值） | 一个需求可拆分多个 FP，每个 FP 都可生成影响说明 |
| D2 | 同 `fp_id` 重新生成时覆盖旧条目 | 避免重复堆积，保留最新分析 |
| D3 | 自动生成用 LLM 合成 description | 用户要求"自动生成+可编辑"，纯规则拼接质量差 |
| D4 | FP 影响分析独立方法 `_analyze_fp_impact`，不修改原 `analyze()` | 原 `analyze()` 依赖 `optimization_context`，FP 影响分析应独立触发 |
| D5 | 低级 `except: pass`（clarify_agent 8处 + crypto 1处）保持现状 | 非关键路径，避免过度工程化 |
| D6 | 前端用 `onNodeDoubleClick` 触发 Modal（复用现有 hook） | 避免新增 `onNodeClick` 冲突 |
| D7 | 不新增 DB 列，`impact_analysis` 存在 `srr_json` JSON 字段内 | 符合用户"需求SRR新增字段"要求，无需 migration |

---

## 五、验证清单

- [ ] 步骤 1：5 处静默吞异常改为 `logger.warning`
- [ ] 步骤 2：SRR_SCHEMA_GUIDE 含 `impact_analysis` 字段说明
- [ ] 步骤 3：`_analyze_fp_impact()` 方法 + LLM 合成方法
- [ ] 步骤 4：POST + PUT 端点，权限 `require_write`
- [ ] 步骤 5：`build_user_prompt()` 注入 `impact_analysis_section`
- [ ] 步骤 6：client.ts 新增 2 个方法
- [ ] 步骤 7：`ImpactDescriptionModal` 组件 + 双击触发
- [ ] 步骤 8：Docker 构建通过
- [ ] 步骤 9：第一轮全链路测试通过
- [ ] 步骤 10：第二轮优化需求 + FP 影响分析测试通过

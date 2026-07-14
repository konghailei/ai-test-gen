# 需求分析进度精细化 + 任务管理 + 异步化改造 + REQ 分析修复 - 实施计划

> 本计划基于 Phase 1 代码探索和 Phase 2 用户澄清，针对四项诉求：
>
> 1. 需求分析时进度条显示更精细（当前是纯前端 setInterval 模拟，与后端真实进度无关）
> 2. 右上角新增任务管理按钮，页面刷新后可在任务管理查看进行中任务
> 3. 需求列表显示该需求，状态为"分析中"
> 4. 检查并修复 REQ-1783990996079-1595 的分析结果（`_baseline_gaps` 硬编码登录数据污染）
>
> 用户确认：后端改为异步任务模式；任务范围覆盖所有长耗时任务；项目已迁移到 PostgreSQL（SQLite 仅遗留，不可用）。

***

## 一、当前状态分析

### 1.1 前端进度条 - 纯模拟，与后端无关

[Requirements.tsx L242-284](file:///d:/git_down/ai-test-gen/frontend/src/pages/Requirements.tsx#L242) 的 `startProgressSimulation` 使用 `setInterval` 每 1.2 秒推进，阶段切换基于 `elapsed` 计数器，**完全不反映后端真实进度**。后端耗时数十秒到数分钟，前端只能在阶段 2（"检测不清晰内容"）后显示"⏳ 正在等待 AI 完成分析..."无限等待。

### 1.2 后端 upload 同步阻塞

[requirements.py L88-296](file:///d:/git_down/ai-test-gen/backend/app/api/requirements.py#L88) `upload_requirement` 同步执行：

1. `req_agent.parse()` - LLM 解析（10-30 秒）
2. `clarify_agent.clarify()` - KB 检索 + LLM 三轮整合（20-60 秒）
3. 可能的 `final_srr = req_agent.parse()` - 再次 LLM 解析（10-30 秒）

HTTP 请求持续 40-120 秒，页面刷新会中断请求，无法恢复。

### 1.3 无任务模型，无 analyzing 状态

* 数据库无 Task/Job 模型

* [Requirement.status](file:///d:/git_down/ai-test-gen/backend/app/db/models.py#L42) 仅有 active/superseded/deprecated/archived/draft，**无 analyzing**

* 前端 STATUS\_LABELS 无 analyzing 标签

### 1.4 右上角导航栏

[Layout.tsx L156-228](file:///d:/git_down/ai-test-gen/frontend/src/components/Layout.tsx#L156) 右上角有"用户手册"按钮和用户信息下拉，可在此插入"任务管理"按钮。

### 1.5 长耗时端点清单

| 端点                           | 文件:行                 | 耗时      | 现状            |
| ---------------------------- | -------------------- | ------- | ------------- |
| upload\_requirement          | requirements.py:88   | 40-120s | 同步阻塞，无进度      |
| submit\_clarify\_answers     | requirements.py:866  | 20-60s  | 同步阻塞，无进度      |
| regenerate\_requirement      | requirements.py:1158 | 10-30s  | 同步阻塞，无进度      |
| analyze\_requirement\_impact | requirements.py:1626 | 10-30s  | 同步阻塞，无进度      |
| generate\_test\_cases        | generate.py:48       | 变化大     | SSE 流式，已有实时进度 |

### 1.6 REQ-1783990996079-1595 分析结果问题

[req\_agent.py L290-298](file:///d:/git_down/ai-test-gen/backend/app/agents/req_agent.py#L290) 的兜底逻辑：

```python
if not srr.get("_is_incremental") and raw_requirement:
    if self._looks_like_incremental(raw_requirement):
        srr["_is_incremental"] = True
        srr.setdefault("_baseline_gaps", [
            "账号密码登录主流程", "密码复杂度与长度规则",
            "登录失败锁定策略", "登录错误提示规范", "验证码发送与校验规则",
        ])
```

* `_looks_like_incremental`（L302-322）的正则 `v\d+\.\d+` 匹配了架构文档中的"版本: v2.0"，误触发

* 硬编码的 5 条**登录相关**缺口被错误注入到**平台架构**需求，属模拟数据污染

***

## 二、拟议变更

### 变更组 A：新增 AnalysisTask 模型（后端基础设施）

**文件 1**: [backend/app/db/models.py](file:///d:/git_down/ai-test-gen/backend/app/db/models.py)

新增任务模型，覆盖所有长耗时任务类型：

```python
class AnalysisTask(Base):
    """异步分析任务（需求解析/澄清/重生成/影响分析/用例生成等长耗时任务）。"""
    __tablename__ = "analysis_tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # TASK-{ms}-{seq}
    task_type: Mapped[str] = mapped_column(String(32), index=True)
    # upload_requirement / submit_clarify / regenerate / analyze_impact / generate_cases
    req_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("requirements.id"), nullable=True, index=True)
    product_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # pending / running / completed / failed / interrupted
    phase: Mapped[str] = mapped_column(String(32), default="")
    # parse / kb_search / clarify / regenerate / impact / generate / review / format / done
    progress: Mapped[int] = mapped_column(Integer, default=0)  # 0-100
    message: Mapped[str] = mapped_column(Text, default="")
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)  # 任务结果（req_id/questions 等）
    error: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="api")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

**文件 2**: [backend/app/db/database.py](file:///d:/git_down/ai-test-gen/backend/app/db/database.py)

* import 添加 `AnalysisTask`

* `init_db` 中 `Base.metadata.create_all` 自动建表

* 启动时清理 stale 任务：将所有 status="running" 的任务标记为 "interrupted"（进程重启后旧 running 任务无法继续）

**文件 3**: [backend/app/utils/id\_gen.py](file:///d:/git_down/ai-test-gen/backend/app/utils/id_gen.py)

新增 `gen_task_id()` 函数（与 gen\_requirement\_id 同模式）。

***

### 变更组 B：upload\_requirement 改为异步（后端核心改造）

**文件**: [backend/app/api/requirements.py](file:///d:/git_down/ai-test-gen/backend/app/api/requirements.py)

**改造方案**：

1. `upload_requirement` 端点改为**轻量入口**：

   * 校验文件/参数

   * 读取文件内容并合并（DocMergeAgent）

   * 立即创建 Requirement 记录（status="analyzing"）

   * 创建 AnalysisTask 记录（status="pending"）

   * 用 `asyncio.create_task` 启动后台协程 `_run_upload_analysis(task_id, req_id, ...)`

   * 立即返回 `{ task_id, req_id, status: "analyzing" }`

2. 新增后台协程 `_run_upload_analysis`：

   * 更新 Task.status="running", phase="parse"

   * 调用 `req_agent.parse()`

   * 更新 phase="kb\_search"

   * 调用 `clarify_agent.clarify()`

   * 根据是否有未解决问题：

     * 有问题：phase="clarify", status="completed"（等待用户澄清），result\_json 含 questions

     * 无问题：phase="regenerate", 调用 `req_agent.parse()` 生成 final\_srr，更新 Requirement.status="active"

   * 更新 Task.status="completed", progress=100, completed\_at=now

   * 异常：Task.status="failed", error=str(e)

3. Requirement.status 新增 "analyzing" 值

```python
@router.post("/upload", response_model=ApiResponse, dependencies=[Depends(require_write)])
async def upload_requirement(req: RequirementUploadRequest, session: AsyncSession = Depends(get_session)):
    # ... 校验 ...
    # 立即创建 Requirement（status=analyzing）
    record = Requirement(id=req_id, status="analyzing", ...)
    session.add(record)
    # 创建任务
    task = AnalysisTask(id=task_id, task_type="upload_requirement", req_id=req_id, ...)
    session.add(task)
    await session.commit()
    # 启动后台协程
    asyncio.create_task(_run_upload_analysis(task_id, req_id, req.product_id, req.raw_text, ...))
    return ApiResponse(success=True, data={"task_id": task_id, "req_id": req_id, "status": "analyzing"})
```

**关键点**：

* 后台协程用独立的 AsyncSession（不能复用请求的 session，请求结束后 session 关闭）

* 协程内所有 DB 操作用新 session

* 协程内更新 Task.phase 供前端轮询

***

### 变更组 C：其他长耗时端点改异步（后端）

对 submit\_clarify\_answers、regenerate\_requirement、analyze\_requirement\_impact 应用相同模式：

**requirements.py**：

* `submit_clarify_answers`（L866）：创建 task\_type="submit\_clarify"，Requirement.status 更新为 "analyzing"，后台执行 final\_srr 生成

* `regenerate_requirement`（L1158）：创建 task\_type="regenerate"，后台执行

* `analyze_requirement_impact`（L1626）：创建 task\_type="analyze\_impact"，后台执行

**generate.py**：

* `generate_test_cases`（L48）：已有 SSE 流式进度，**保持流式**，但额外创建 AnalysisTask 记录（status="running"），完成后更新为 completed。任务管理可见，但不改变流式体验。

***

### 变更组 D：任务状态查询 API（后端）

**文件**: [backend/app/api/requirements.py](file:///d:/git_down/ai-test-gen/backend/app/api/requirements.py)

新增端点：

```python
@router.get("/tasks", response_model=ApiResponse)
async def list_tasks(
    status: str | None = Query(None),  # running/completed/failed
    task_type: str | None = Query(None),
    product_id: str | None = Query(None),
    limit: int = Query(50),
    offset: int = Query(0),
    session: AsyncSession = Depends(get_session),
):
    """列出分析任务（支持按状态/类型/产品筛选，分页）。"""

@router.get("/tasks/{task_id}", response_model=ApiResponse)
async def get_task(task_id: str, session: AsyncSession = Depends(get_session)):
    """查询单个任务状态（供前端轮询进度）。"""
    # 返回 { id, task_type, req_id, status, phase, progress, message, result_json, error, ... }

@router.get("/{req_id}/task", response_model=ApiResponse)
async def get_task_by_req(req_id: str, session: AsyncSession = Depends(get_session)):
    """按需求 ID 查询关联的最新任务（供需求详情页恢复进度）。"""
```

***

### 变更组 E：前端进度条改为真实进度（轮询）

**文件 1**: [frontend/src/api/client.ts](file:///d:/git_down/ai-test-gen/frontend/src/api/client.ts)

新增任务 API：

```typescript
requirements: {
  // ... 现有方法 ...
  listTasks: (params?: { status?: string; task_type?: string }) =>
    request<{ items: AnalysisTask[]; total: number }>('/requirements/tasks', { method: 'GET', params }),
  getTask: (taskId: string) =>
    request<AnalysisTask>(`/requirements/tasks/${taskId}`),
  getTaskByReq: (reqId: string) =>
    request<AnalysisTask | null>(`/requirements/${reqId}/task`),
}
```

新增 AnalysisTask 类型（types/index.ts）。

**文件 2**: [frontend/src/pages/Requirements.tsx](file:///d:/git_down/ai-test-gen/frontend/src/pages/Requirements.tsx)

改造 `handleMultiFileUpload`：

```typescript
const handleMultiFileUpload = async () => {
  // ... 校验 ...
  // 调用 upload，立即返回 task_id
  const res = await reqApi.uploadMultiFiles(...)
  const { task_id, req_id } = res.data
  // 开始轮询任务状态
  startTaskPolling(task_id, req_id)
}

const startTaskPolling = (taskId: string, reqId: string) => {
  // 每 1.5 秒轮询 GET /requirements/tasks/{taskId}
  // 更新 progress state（phase + message + progress%）
  // status=completed 且 result_json 含 questions → 显示澄清面板
  // status=completed 且无 questions → runImpactAnalysis
  // status=failed → 显示错误
}
```

保留 UPLOAD\_PHASES 定义，但进度不再模拟，改为从 task.phase 映射到 UPLOAD\_PHASES 索引。

**关键改进**：进度条显示真实 phase 和 progress%（0-100），不再是"⏳ 正在等待"无限等待。

***

### 变更组 F：任务管理按钮 + 任务管理页面（前端）

**文件 1**: [frontend/src/components/Layout.tsx](file:///d:/git_down/ai-test-gen/frontend/src/components/Layout.tsx#L156)

在"用户手册"按钮旁新增"任务管理"按钮：

```tsx
<button
  onClick={() => navigate('/tasks')}
  className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl hover:bg-slate-100 transition-all text-sm text-slate-600"
  title="任务管理"
>
  <ListTodo className="w-4 h-4" />
  <span className="hidden sm:inline">任务管理</span>
  {/* 进行中任务数角标 */}
  {runningTaskCount > 0 && (
    <span className="ml-1 px-1.5 py-0.5 bg-primary-500 text-white text-[10px] rounded-full">
      {runningTaskCount}
    </span>
  )}
</button>
```

新增 `runningTaskCount` state，定期轮询 `/requirements/tasks?status=running&limit=1` 获取数量。

**文件 2**: 新建 `frontend/src/pages/Tasks.tsx`

任务管理页面：

* 任务列表（表格：任务类型/关联需求/状态/进度/创建时间/耗时）

* 状态筛选（全部/进行中/已完成/失败）

* 点击任务行 → 跳转关联需求详情页

* 进行中任务显示实时进度条（轮询更新）

* 失败任务显示错误信息

**文件 3**: [frontend/src/App.tsx](file:///d:/git_down/ai-test-gen/frontend/src/App.tsx)（或路由文件）

新增路由 `/tasks` → Tasks 页面。

***

### 变更组 G：需求列表显示"分析中"状态

**文件 1**: [frontend/src/utils.ts](file:///d:/git_down/ai-test-gen/frontend/src/utils.ts)（或 STATUS\_LABELS 所在文件）

STATUS\_LABELS 新增：

```typescript
export const STATUS_LABELS: Record<string, { label: string; color: string }> = {
  analyzing: { label: '分析中', color: 'blue' },
  draft: { label: '草稿', color: 'amber' },
  active: { label: '已激活', color: 'success' },
  archived: { label: '已归档', color: 'slate' },
  // ...
}
```

**文件 2**: [frontend/src/pages/Requirements.tsx](file:///d:/git_down/ai-test-gen/frontend/src/pages/Requirements.tsx)

需求列表加载时包含 analyzing 状态的需求（status=analyzing 的需求现在可见）。

分析中的需求点击 → 检查是否有关联 running task → 有则恢复进度条，无则提示"分析中断，请重新上传"。

***

### 变更组 H：修复 REQ-1783990996079-1595 的 \_baseline\_gaps 污染

**文件**: [backend/app/agents/req\_agent.py](file:///d:/git_down/ai-test-gen/backend/app/agents/req_agent.py#L290)

**问题根因**：`_looks_like_incremental` 的正则 `v\d+\.\d+` 匹配了架构文档标题中的"v2.0"，触发硬编码的登录相关 `_baseline_gaps` 注入。

**修复方案**：

1. **移除硬编码** **`_baseline_gaps`**：改为留空列表，交由 ClarifyAgent 推导：

```python
if not srr.get("_is_incremental") and raw_requirement:
    if self._looks_like_incremental(raw_requirement):
        srr["_is_incremental"] = True
        # 不再注入硬编码登录缺口，留给 ClarifyAgent 基于原文动态推导
        srr.setdefault("_baseline_gaps", [])
        logger.info("[ReqAgent] 检测到增量文档信号，标记 _is_incremental=true（基线缺口由 ClarifyAgent 推导）")
```

1. **收紧** **`_looks_like_incremental`** **的匹配**（L302-322）：

   * 移除 `v\d+\.\d+` 这个过宽的正则（架构文档版本号会误触发）

   * 仅保留明确的增量信号：`相对于V?d`、`新增与优化`、`本文档仅描述`、`在V\d基础上`等

   * 标题含版本号不再视为增量信号（多文档拼接的架构文档普遍含版本号）

```python
signals = [
    r"相对于\s*V?\d", r"新增与优化", r"完整上下文请参见", r"本文档仅描述",
    r"优化版本", r"在\s*V\d.*基础上", r"基于\s*V\d", r"升级到\s*V\d",
    r"相对于.*版本",
]
# 移除：标题含版本号视为增量信号（误触发率过高）
```

**验证**：修复后对 REQ-1783990996079-1595 的 raw\_text 重新解析，确认 `_baseline_gaps` 为空或仅含原文真实缺口。

***

## 三、假设与决策

### 假设

1. **异步执行机制**：使用 `asyncio.create_task`（FastAPI 进程内协程），适配 async LLM 调用，无需 Celery 的 async/sync 桥接。进程重启时 stale 任务标记为 interrupted。
2. **任务持久化**：AnalysisTask 表存储在 PostgreSQL（项目已迁移 PG，SQLite 不可用）。
3. **进度粒度**：phase 级别（parse/kb\_search/clarify/regenerate/impact/done），每个 phase 内 progress 0-100。不细分到 LLM token 级别（过度工程）。
4. **用例生成保持 SSE**：generate\_test\_cases 已有流式进度，不改流式机制，仅新增 Task 记录供任务管理可见。
5. **后台协程 DB 访问**：使用独立 AsyncSession（不复用请求 session），协程内自行管理 session 生命周期。

### 决策

* **本次重点**：upload\_requirement 完整异步化 + 任务管理基础设施 + 前端真实进度。submit\_clarify/regenerate/impact 按相同模式改造但可复用基础设施。

* **范围限定**：用例生成不改流式，仅加 Task 记录。

* **analyzing 状态**：新增 Requirement.status="analyzing"，分析完成（无论是否有澄清问题）后更新为 draft（有问题）或 active（无问题）。

* **REQ-1783990996079-1595 修复**：移除硬编码 + 收紧正则。不重新解析该需求（用户可在修复后重新上传验证）。

***

## 四、实施顺序

1. **变更组 A**（models.py + database.py + id\_gen.py）-> AnalysisTask 模型 + 自动建表 + stale 清理
2. **变更组 H**（req\_agent.py）-> 修复 \_baseline\_gaps 硬编码（独立修复，优先）
3. **变更组 B**（requirements.py upload\_requirement）-> 核心异步化改造
4. **变更组 C**（requirements.py 其他端点 + generate.py）-> 其余端点异步化
5. **变更组 D**（requirements.py 任务查询 API）-> 任务状态查询端点
6. **变更组 E**（client.ts + Requirements.tsx）-> 前端真实进度轮询
7. **变更组 G**（utils + Requirements.tsx）-> analyzing 状态显示
8. **变更组 F**（Layout.tsx + Tasks.tsx + 路由）-> 任务管理按钮 + 页面

***

## 五、验证步骤

### 5.1 后端验证

1. **AnalysisTask 表创建**：启动后端，确认 PG 中 analysis\_tasks 表存在
2. **upload 异步**：POST /upload 立即返回 task\_id（<1 秒），后台日志显示分析进行中
3. **任务状态查询**：GET /requirements/tasks/{task\_id} 返回正确 phase 和 progress
4. **stale 清理**：重启后端，原 running 任务变 interrupted
5. **\_baseline\_gaps 修复**：对 REQ-1783990996079-1595 的 raw\_text 重新调用 req\_agent.parse，确认 \_baseline\_gaps 为空
6. **analyzing 状态**：上传后 Requirement.status="analyzing"，分析完成后更新为 draft/active

### 5.2 前端验证

1. **真实进度**：上传后进度条显示真实 phase（parse→kb\_search→clarify），progress% 递增
2. **页面刷新恢复**：上传中刷新页面，进度条消失但需求列表显示"分析中"，任务管理可见 running 任务
3. **任务管理**：右上角按钮显示进行中任务数角标，点击进入任务列表
4. **analyzing 显示**：需求列表中分析中需求显示蓝色"分析中"标签
5. **任务列表**：任务管理页显示所有任务，可按状态筛选

### 5.3 REQ-1783990996079-1595 专项验证

1. 修复后重新上传该需求的 8 份文档
2. 确认 \_baseline\_gaps 不含登录相关内容
3. 确认 \_is\_incremental 不被误触发（架构文档版本号不再误判）

***

## 六、风险与回滚

* AnalysisTask 是新增表，不影响现有功能

* upload\_requirement 改异步是破坏性变更（前端需同步改造），但前后端一起部署

* \_baseline\_gaps 修复仅影响增量文档兜底，正常需求不受影响

* 用例生成不改流式，风险低

* 若异步协程出问题，可回退到同步（git revert upload\_requirement 改动）


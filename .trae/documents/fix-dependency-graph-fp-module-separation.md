# 修复依赖图谱：模块依赖与功能点依赖分离

## 摘要

依赖图谱页面（`DependencyGraph.tsx`）虽然已有 `graphType: 'modules' | 'fp'` 切换状态，但 FP 模式下的所有编辑操作（连线/删除/拖拽）都错误地调用了模块专用 API，导致：
- FP 模式下连线/删除调用 `dependencyGraph.addEdge/removeEdge`（模块表），写入错误数据
- FP 节点拖拽调用 `updateNodePosition`（FP 无 pos_x/pos_y 字段，静默失败）
- 边样式只有模块的 2 种（depends_on/interdependent），FP 的 5 种关系类型无样式
- FP 图谱加载存在 N+1 查询 + `slice(0, 10)` 静默截断
- 工具栏/侧边栏/图例在 FP 模式下仍显示模块术语

后端 A1-A2（POST/DELETE FP 依赖端点）已完成，本计划完成剩余的 A3 + 前端分离重构。

## 当前状态分析

### 后端（已完成 A1-A2）
- ✅ `POST /{req_id}/fp/dependencies` ([requirements.py:2276](file:///e:/git-down/ai-test-gen/backend/app/api/requirements.py#L2276)) — 创建 FP 依赖，含 5 种关系类型校验、自引用检查、同产品校验、查重
- ✅ `DELETE /{req_id}/fp/dependencies/{dep_id}` ([requirements.py:2346](file:///e:/git-down/ai-test-gen/backend/app/api/requirements.py#L2346)) — 删除 FP 依赖，含 ownership 校验
- ✅ `GET /{req_id}/fp/dependencies` ([requirements.py:2235](file:///e:/git-down/ai-test-gen/backend/app/api/requirements.py#L2235)) — 单需求的 FP 依赖查询
- ❌ 缺少跨需求 FP 图谱查询端点（A3）

### 前端问题清单（DependencyGraph.tsx 705 行）
| 行号 | 问题 | 影响 |
|------|------|------|
| L57-60 | `EDGE_STYLES` 只有 `depends_on`/`interdependent` | FP 的 5 种关系类型无样式 |
| L64-81 | `createEdge()` 硬编码模块术语 | FP 边标签错误 |
| L19-50 | `ModuleNode` 同时用于模块和 FP | FP 缺少类型徽标 |
| L245 | `reqItems.slice(0, 10)` | 静默截断，丢失 FP |
| L263-278 | 按需求循环调用 `fpDependencies` | N+1 查询 |
| L288-297 | `onConnect` 总是调 `dependencyGraph.addEdge` | FP 模式写入模块表 |
| L299-310 | `deleteEdge` 总是调 `dependencyGraph.removeEdge` | FP 模式删除模块边 |
| L344-346 | `onNodeDragStop` 总是调 `updateNodePosition` | FP 无此字段，静默失败 |
| L432-444 | `swapEdge` 总是用模块 API | FP 模式错误 |
| L470-481 | 工具栏模块拖拽项在 FP 模式仍显示 | 误导用户 |
| L515-524 | 边类型选择器只显示模块类型 | FP 无法选 5 种关系 |
| L529 | 刷新按钮总调 `loadGraph()` | FP 模式刷新错误 |
| L540-582 | 侧边栏/图例总显示模块术语 | 误导 |

### 前端 API 客户端（client.ts）
- L249 `fpDependencies(reqId)` — 仅 GET，缺 POST/DELETE
- L567-589 `dependencyGraph` — 模块专用，无 FP 图谱方法

## 假设与决策

1. **FP 节点位置不持久化**：FunctionalPoint 模型无 pos_x/pos_y 字段，FP 模式下拖拽仅本地生效（刷新后回 auto-layout）。模块模式仍持久化。
2. **FP 图谱显示所有有 FP 的需求**：不限于 archived 状态（draft/active/archived 均可），只要需求下有 FunctionalPoint 即显示。
3. **FP 关系类型用中文标签**：depends_on→依赖、data_flows→数据流向、invariants→共享约束、lifecycle_scenarios→生命周期、conflicts_with→冲突。
4. **跨需求 FP 依赖**：A3 端点返回产品下所有 FP 节点 + 所有 FPDependency 边（含跨需求边）。

## 变更组 A：后端（A3 新增）

### A3: 跨需求 FP 图谱查询端点
**文件**: `backend/app/api/products.py`（在现有 `/dependency-graph` 端点后新增）

**端点**: `GET /products/{product_id}/fp/dependency-graph`

**逻辑**:
1. 查询该产品下所有需求的 ID 集合（不限状态，只要 requirement.product_id == product_id）
2. 查询这些需求下的所有 FunctionalPoint（id, name, fp_type, requirement_id, priority）
3. 关联 Requirement.title 用于节点描述
4. 查询所有 FPDependency（source_fp_id 或 target_fp_id 在上述 FP 集合内）
5. 返回 `{ nodes: [...], edges: [...] }`

**响应格式**:
```json
{
  "nodes": [
    {"id": "FP-xxx", "name": "用户登录", "fp_type": "workflow", "req_id": "REQ-xxx", "req_title": "登录模块", "priority": "P1"}
  ],
  "edges": [
    {"id": 1, "source": "FP-xxx", "target": "FP-yyy", "relation_type": "depends_on", "description": "..."}
  ]
}
```

**注意**: 边的 source/target 字段名用 `source`/`target`（与模块图谱一致），便于前端复用 ReactFlow 节点/边构建逻辑。

## 变更组 B：前端 API 客户端

**文件**: `frontend/src/api/client.ts`

### B1: 在 `requirements` 对象内新增 FP 依赖写方法
在 L250 `fpDependencies` 后新增：
```typescript
fpAddDependency: (reqId: string, sourceFpId: string, targetFpId: string, relationType: string, description = '') =>
  request<{ id: number }>(`/requirements/${reqId}/fp/dependencies`, {
    method: 'POST',
    body: JSON.stringify({ source_fp_id: sourceFpId, target_fp_id: targetFpId, relation_type: relationType, description }),
  }),
fpRemoveDependency: (reqId: string, depId: number) =>
  request(`/requirements/${reqId}/fp/dependencies/${depId}`, { method: 'DELETE' }),
```

### B2: 在 `dependencyGraph` 对象内新增 FP 图谱方法
在 L588 `analyzeProject` 后新增：
```typescript
fpGraph: (productId: string) =>
  request<{ nodes: any[]; edges: any[] }>(`/products/${productId}/fp/dependency-graph`),
```

## 变更组 C：前端 DependencyGraph.tsx 重构

**文件**: `frontend/src/pages/DependencyGraph.tsx`

### C1: 分离边样式
将 L57-60 的 `EDGE_STYLES` 拆为两个常量：
```typescript
const MODULE_EDGE_STYLES: Record<string, { label: string; stroke: string }> = {
  depends_on: { label: '依赖', stroke: '#818cf8' },
  interdependent: { label: '相互依赖', stroke: '#22d3ee' },
}
const FP_EDGE_STYLES: Record<string, { label: string; stroke: string }> = {
  depends_on: { label: '依赖', stroke: '#818cf8' },
  data_flows: { label: '数据流向', stroke: '#34d399' },
  invariants: { label: '共享约束', stroke: '#fbbf24' },
  lifecycle_scenarios: { label: '生命周期', stroke: '#f472b6' },
  conflicts_with: { label: '冲突', stroke: '#f87171' },
}
```
修改 `createEdge()` 接受 `styles` 参数，按 graphType 传入对应样式表。所有引用 `EDGE_STYLES` 的位置改为按 graphType 选择。

### C2: 新增 FPNode 组件
在 `ModuleNode` 后新增 `FPNode` 组件：
- 显示 fp_type 徽标（不同颜色：workflow=蓝、calculation=绿、constraint=橙、api_behavior=紫、data_record=青、config=灰）
- 副标题显示 `[需求标题前 20 字] · fp_type`
- 注册到 `nodeTypes`: `{ moduleNode: ModuleNode, fpNode: FPNode }`

### C3: onConnect 按 graphType 路由
```typescript
const onConnect = useCallback(async (conn: Connection) => {
  if (!conn.source || !conn.target || conn.source === conn.target) return
  try {
    if (graphType === 'fp') {
      // FP 模式：需确定 source_fp 所属的 req_id
      const sourceNode = nodes.find(n => n.id === conn.source)
      const reqId = sourceNode?.data?.reqId
      if (!reqId) { alert('无法确定功能点所属需求'); return }
      await reqApi.fpAddDependency(reqId, conn.source, conn.target, edgeType)
      await loadFPGraph()
    } else {
      await dependencyGraph.addEdge(conn.source, conn.target, edgeType)
      await loadGraph()
    }
  } catch (e: any) {
    alert('添加依赖失败: ' + (e?.message || '未知错误'))
  }
}, [edgeType, graphType, nodes])
```
**关键**: FP 节点的 `data.reqId` 在 C9 加载时写入，用于此处确定 POST 端点的 req_id 路径参数。

### C4: deleteEdge 按 graphType 路由
```typescript
const deleteEdge = async (edgeId: string) => {
  const edge = edges.find(e => e.id === edgeId)
  if (!edge || !confirm('确认删除该连线？')) return
  try {
    if (graphType === 'fp') {
      const depId = Number(String(edge.id).replace(/^e-/, ''))
      const sourceNode = nodes.find(n => n.id === edge.source)
      const reqId = sourceNode?.data?.reqId
      if (!reqId || !depId) { alert('无法删除：缺少依赖 ID'); return }
      await reqApi.fpRemoveDependency(reqId, depId)
      await loadFPGraph()
    } else {
      await dependencyGraph.removeEdge(String(edge.source), String(edge.target))
      await loadGraph()
    }
    setSelectedEdge(null)
  } catch (e: any) {
    alert('删除失败: ' + (e?.message || '未知错误'))
  }
}
```
**关键**: FP 边的 id 在加载时设为 `e-{dep.id}`（C9），此处解析回 dep_id。

### C5: onNodeDragStop 在 FP 模式跳过
```typescript
const onNodeDragStop = useCallback(async (_: any, node: Node) => {
  if (graphType === 'fp') return  // FP 不持久化位置
  try { await dependencyGraph.updateNodePosition(node.id, node.position.x, node.position.y) } catch { }
}, [graphType])
```

### C6: swapEdge 按 graphType 路由
`swapEdge` 函数（L432-444）按 graphType 分支：
- 模块模式：保持现有逻辑（removeEdge + addEdge）
- FP 模式：删除原 dep + 创建反向 dep（fpRemoveDependency + fpAddDependency），需用 source 节点的 reqId

### C7: 工具栏按模式显示
- L470-481 模块拖拽项 + 连线拖拽项：仅 `graphType === 'modules'` 时显示
- L515-524 边类型选择器：按 graphType 渲染不同选项
  - 模块模式：depends_on / interdependent
  - FP 模式：depends_on / data_flows / invariants / lifecycle_scenarios / conflicts_with
- L529 刷新按钮：`onClick={() => graphType === 'fp' ? loadFPGraph() : loadGraph()}`
- `edgeType` 默认值重置：切换 graphType 时重置为该模式的第一个类型

### C8: 侧边栏按模式显示
- L546-558 统计：FP 模式显示「功能点节点」「依赖连线」（不显示「可用模块」）
- L562-572 图例：按 graphType 使用 `MODULE_EDGE_STYLES` 或 `FP_EDGE_STYLES`
- L576-578 提示文字：FP 模式改为「拖拽节点手柄连线 · 右键删除/交换方向」

### C9: 重构 loadFPGraph 使用新端点
替换 L234-285 整个函数：
```typescript
const loadFPGraph = async () => {
  if (!selectedProductId) return
  setLoading(true)
  try {
    const data = await dependencyGraph.fpGraph(selectedProductId)
    const fpNodes: Node[] = (data?.nodes || []).map((n: any, i: number) => ({
      id: n.id,
      type: 'fpNode',
      position: autoLayout(i, (data?.nodes || []).length),
      data: {
        label: n.name,
        fpType: n.fp_type,
        reqId: n.req_id,           // 供 onConnect/deleteEdge 使用
        reqTitle: n.req_title,
        priority: n.priority,
      },
    }))
    const nameMap: Record<string, string> = {}
    fpNodes.forEach(n => { nameMap[n.id] = String(n.data.label) })
    const fpEdges: Edge[] = (data?.edges || []).map((e: any) =>
      createFPEdge(e.id, e.source, e.target, e.relation_type, nameMap[e.source], nameMap[e.target], e.description)
    )
    setNodes(fpNodes)
    setEdges(fpEdges)
  } catch { }
  setLoading(false)
}
```
**新增 `createFPEdge` 辅助函数**：用 `FP_EDGE_STYLES` 生成边，id 格式 `e-{dep.id}`，label 用中文关系名。

### C10: 切换 graphType 时重置 edgeType
在 L470-481 的切换按钮 onClick 中，切换后重置 edgeType：
```typescript
onClick={() => { setGraphType('modules'); setEdgeType('depends_on') }}
onClick={() => { setGraphType('fp'); setEdgeType('depends_on') }}
```

### C11: 移除 FP 模式下的模块专属操作
- `openAddPicker` / `addNodeToGraph` / `swapNodeModule` / `onNodeDoubleClick` / `onDrop`：FP 模式下不触发或禁用
  - 最简实现：在 FP 模式下双击/拖放不打开 picker（FP 节点由数据驱动，不支持手动添加）
  - `onNodeDoubleClick` 增加 `if (graphType === 'fp') return`
  - `onDrop` 增加 `if (graphType === 'fp') return`

## 验证步骤

1. **后端 A3 验证**:
   ```powershell
   curl.exe http://localhost:8000/api/products/PROD-DEFAULT/fp/dependency-graph -H "Authorization: Bearer <token>"
   ```
   确认返回 `{ nodes: [...], edges: [...] }` 结构

2. **前端构建**:
   ```powershell
   cd e:\git-down\ai-test-gen\frontend; npm run build
   ```
   确认无 TypeScript 错误

3. **容器重建**:
   ```powershell
   cd e:\git-down\ai-test-gen; docker compose up -d --build backend frontend
   ```

4. **功能验证（浏览器）**:
   - 模块依赖模式：连线/删除/拖拽位置持久化 → 正常
   - FP 依赖模式：
     - 连线 → 调用 `fpAddDependency`，刷新后仍存在
     - 删除 → 调用 `fpRemoveDependency`，刷新后已删除
     - 拖拽位置 → 本地生效，刷新后回 auto-layout
     - 边类型选择器 → 显示 5 种 FP 关系类型
     - 侧边栏 → 显示「功能点节点」「依赖连线」+ FP 图例
     - 工具栏 → 无模块拖拽项
   - 切换模式 → edgeType 重置，图谱正确加载

## 实施顺序

1. 变更组 A：后端 A3 端点
2. 变更组 B：前端 API 客户端 B1-B2
3. 变更组 C：前端 DependencyGraph.tsx 重构（C1-C11）
4. 构建并验证

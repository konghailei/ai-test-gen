# 用户手册重构 + 浏览器冒烟测试计划

## 概述

将用户手册从弹窗（Modal）重构为独立页面：左侧功能菜单 + 右侧详细操作手册（含实现逻辑说明）。随后对所有新功能进行浏览器端冒烟测试。

## 当前状态分析

### 用户手册现状
- **文件**: [UserManualModal.tsx](file:///e:/git-down/ai-test-gen/frontend/src/components/UserManualModal.tsx) — 居中弹窗，6 个 section 垂直排列在一个滚动容器中
- **触发方式**: [Layout.tsx](file:///e:/git-down/ai-test-gen/frontend/src/components/Layout.tsx) L158-165 顶部栏"用户手册"按钮 → `setShowManual(true)` → 渲染 `<UserManualModal>`
- **内容深度**: 每个 item 只有 `label` + 1 行 `desc`，缺乏实现逻辑和技术细节
- **用户反馈**: "位置好像不太对" + "不是弹窗而是左边是功能菜单，点击对应菜单显示对应操作手册" + "实现逻辑说明要详细一点"

### 路由结构
- 主系统路由在 [App.tsx](file:///e:/git-down/ai-test-gen/frontend/src/App.tsx) L75-95，包裹在 `<Layout />` 下
- 需要新增 `/manual` 路由，使其共享顶部栏和左侧导航栏

## 变更计划

### 任务 1: 新建 UserManual 页面（替换 Modal）

**文件**: `frontend/src/pages/UserManual.tsx`（新建）

**布局结构**:
```
┌─────────────────────────────────────────────────┐
│  顶部栏（Layout 提供）                            │
├──────────────┬──────────────────────────────────┤
│  左侧功能菜单  │  右侧内容区                       │
│              │                                  │
│  ● 需求管理   │  # 需求管理                        │
│  ○ 用例生成   │                                  │
│  ○ 知识库     │  ## 功能说明                       │
│  ○ 项目与模块 │  - 上传需求：...                  │
│  ○ 管理后台   │  - 自动解析：...                  │
│  ○ KB检索规则 │                                  │
│              │  ## 实现逻辑                       │
│              │  1. ReqAgent 调用 LLM...          │
│              │  2. 解析为 SRR JSON...            │
│              │                                  │
│              │  ## 关键配置                       │
│              │  - LLM 供应商：系统设置 → ...      │
│              │  - 相似度阈值：0.40               │
└──────────────┴──────────────────────────────────┘
```

**左侧菜单**:
- 6 个功能项，每项含 icon + 标题
- 点击切换右侧内容，选中项高亮（`bg-primary-50 text-primary-700`）
- 宽度 `w-56`，与主 Sidebar 风格一致

**右侧内容**: 每个 section 包含 3 个层次：
1. **功能说明** — 列出每个子功能 + 简要描述（保留现有内容）
2. **实现逻辑** — 详细的技术实现说明（新增），包括：
   - 数据流（用户操作 → API → Agent → DB/向量库）
   - 关键算法/阈值
   - 涉及的后端文件和函数
3. **关键配置** — 相关的系统配置项、参数说明（新增）

**6 个 section 的详细实现逻辑内容**:

#### 1. 需求管理
- **数据流**: 上传文件 → `POST /requirements/upload/file` → `ReqAgent.parse()` 调用 LLM → 生成 SRR JSON → 存入 PostgreSQL
- **KB 补全**: `ClarifyAgent._query_kb_for_answers()` 检索 Milvus（相似度 > 0.40）→ LLM 二次校验 → 自动补全缺失字段
- **人工澄清**: 动态生成问题（KB 查询、缺失维度、字段冲突）→ 用户回答 → `POST /requirements/{id}/clarify` → 重新生成 SRR
- **重新生成**: `POST /requirements/{id}/regenerate` — 即使 status=active 也可调用，操作记录存入 `srr_json._clarify_history`
- **归档**: `POST /requirements/{id}/archive` — 设置 status=archived，写入 Milvus，元数据含 go_live_date + project_version

#### 2. 测试用例生成
- **数据流**: `POST /generate` (SSE) → `GenAgent.generate()` → 逐条流式输出用例 → 存入 PostgreSQL
- **模式复用**: 从 Milvus `test_patterns` 集合检索匹配的测试模式（CRUD/状态机/认证等 8 种种子模式）
- **影响分析**: `ImpactAgent` 基于模块依赖图谱 BFS 遍历，识别直接/间接受影响模块
- **审核**: `ReviewAgent` 对生成用例做质量评分

#### 3. 知识库
- **架构**: Milvus（向量检索）+ MinIO（Milvus 的 S3 存储后端）+ PostgreSQL（结构化数据）
- **归档入库**: 需求归档时 → `vector_store.upsert_requirement()` → Milvus `requirements` 集合
- **去重规则**: 同功能多版本时按 `go_live_date` 降序排序，靠后日期优先
- **状态指示**: 绿色"已归档"= PostgreSQL status=archived；蓝色"已向量化"= Milvus 中存在该向量

#### 4. 项目与模块
- **标签页**: 项目/模块分 Tab 展示，各自独立搜索（`useMemo` 过滤 name/description/version）
- **版本号**: 项目创建时填写，存入 `Project.version` 列，归档时随需求写入 Milvus 元数据
- **依赖图谱**: `DependencyGraph` 可视化模块依赖，支持拖拽编辑连线

#### 5. 管理后台
- **产品管理**: 创建/删除产品，配置可见性
- **用户/角色**: RBAC 权限模型，角色关联菜单权限
- **系统设置**: LLM 供应商配置（多供应商 + 主备），持久化到 PostgreSQL
- **提示词管理**: 各 Agent 的 Prompt 模板，支持版本控制和恢复默认
- **KB 诊断**: `GET /knowledge/diagnostics` — Milvus 连接状态、集合数量、架构说明

#### 6. KB 检索规则
- **Embedding 模型**: bge-large-zh（本地部署，基础镜像内置）
- **相似度阈值**: 0.40（问题→文档语义匹配的典型 cosine 相似度为 0.40-0.65）
- **二次校验**: LLM 对 KB 检索结果做相关性验证，过滤误匹配
- **状态过滤**: 仅检索 `status=archived` 的需求
- **版本溯源**: 检索结果标注 `project_version` + `go_live_date`，如"（v2.1, 上线: 2026-07-11, 相似度 0.55）"
- **排序规则**: `go_live_date` 降序 → 相似度降序

### 任务 2: 注册路由

**文件**: `frontend/src/App.tsx`

**变更**: 在 Layout 路由组中新增：
```tsx
import UserManual from './pages/UserManual'
// ...
<Route path="/manual" element={<UserManual />} />
```

### 任务 3: 更新 Layout.tsx

**文件**: `frontend/src/components/Layout.tsx`

**变更**:
1. 删除 `import UserManualModal from './UserManualModal'`
2. 删除 `const [showManual, setShowManual] = useState(false)`
3. 删除底部 `{showManual && <UserManualModal onClose={() => setShowManual(false)} />}`
4. 将"用户手册"按钮的 `onClick` 从 `setShowManual(true)` 改为 `navigate('/manual')`
5. 添加 `useNavigate`（已存在，L12）

### 任务 4: 删除旧文件

**文件**: `frontend/src/components/UserManualModal.tsx` — 删除（被 UserManual.tsx 替代）

### 任务 5: 浏览器冒烟测试

使用 playwright 测试以下功能：

1. **用户手册页面**:
   - 从顶部栏点击"用户手册" → 跳转到 `/manual`
   - 左侧菜单 6 个项，点击切换右侧内容
   - 验证右侧内容含"功能说明"/"实现逻辑"/"关键配置"三个层次

2. **归档日期选择器**:
   - 进入需求详情页 → 点击"归档" → 弹出日期选择器
   - 验证日期必填（不选日期时"确认归档"按钮 disabled）
   - 验证提示文案"上线日期用于知识库检索时区分最新需求"

3. **项目/模块标签页 + 搜索**:
   - 进入项目与模块页面
   - 验证"项目"/"模块"两个 Tab 可切换
   - 在搜索框输入文字，验证列表实时过滤
   - 切换 Tab 验证搜索框独立（项目搜索不影响模块）

4. **版本号显示**:
   - 在项目 Tab 点击"添加项目"
   - 验证表单含版本号输入框（默认 1.0）
   - 添加项目后验证列表中显示 `v{版本号}` 徽章

## 假设与决策

1. **路由位置**: `/manual` 放在 Layout 路由组内（共享顶部栏和左侧导航），不放 AdminLayout（普通用户也可访问）
2. **左侧菜单宽度**: `w-56`（224px），与主 Sidebar 的 `w-60` 略窄，视觉区分
3. **不做 i18n**: 内容直接硬编码中文，与现有 UserManualModal 一致
4. **不保留弹窗模式**: 完全替换为页面，不保留旧的 Modal 组件
5. **实现逻辑内容**: 基于项目记忆中的工程约定和经验教训编写，引用真实的阈值、文件名、函数名

## 验证步骤

1. `tsc --noEmit` 类型检查通过
2. `npm run build` 构建成功
3. Docker 重建前端容器
4. Playwright 浏览器测试覆盖全部 4 个冒烟测试场景
5. 验证旧 UserManualModal.tsx 已删除，无残留引用

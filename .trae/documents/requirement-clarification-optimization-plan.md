# 需求澄清链路优化实施计划

> 基于用户描述的 7 步工作流（上传 → LLM 分析缺口 → 生成问题 → 查 KB → KB 答出初稿 SRR → 人工补答 → 重生成 SRR），
> 对齐 `docs/01-requirement-parsing.md` 设计文档，参考 Trae 提问交互体验，对系统进行可用性优化。

---

## 一、摘要

### 目标
1. **澄清交互体验优化**（参考 Trae 提问功能）：分组展示、进度跟踪、KB 补全可视化、草稿自动保存、单题跳过
2. **KB 检索与补全质量提升**：实现字段冲突优先级规则（§4.4.1），让 KB 补全有来源追踪与冲突处理
3. **增强已设计但未实现的能力**：需求模式库（§6）、冷启动深度问题（§4.5）
4. **保证可行性**：所有改动基于现有架构增量扩展，不破坏已工作的链路

### 范围说明
- **P1 数据质量 Bug（Bug 1/2/6）已在代码中修复**（经代码核实，`req_agent.py` L323-324/L486-499、`milvus_store.py` L202-204 均有修复注释），本计划不再重复处理
- 多轮迭代（§4.5 最多 2 轮）为 SpecFix 2025 论文刻意简化为单轮，保持现状不实现
- 落地范围：完整对齐设计文档中「字段冲突优先级」「需求模式库」「冷启动深度问题」「一次性问题列表交互」四项

---

## 二、当前状态分析

### 2.1 已修复（无需改动）

| Bug | 文件 | 状态 | 证据 |
|-----|------|------|------|
| Bug 1 (P1) name→field 别名过宽 | [req_agent.py](file:///e:/git-down/ai-test-gen/backend/app/agents/req_agent.py#L323) | ✅ 已修复 | L323-324 注释「不再把通用 key name 映射为 field」 |
| Bug 2 (P1) AC.type 重置 | [req_agent.py](file:///e:/git-down/ai-test-gen/backend/app/agents/req_agent.py#L486) | ✅ 已修复 | L491-499 `AC_TYPE_VALID` 完整白名单含 performance/security |
| Bug 6 (P2) Milvus 删除表达式 | [milvus_store.py](file:///e:/git-down/ai-test-gen/backend/app/db/milvus_store.py#L202) | ✅ 已修复 | L202-204 用 `json.dumps` 生成双引号字面量 |

### 2.2 后端已实现（无需改动）

| 能力 | 文件 | 状态 |
|------|------|------|
| LLM 驱动 KB 查询生成 | [clarify_agent.py](file:///e:/git-down/ai-test-gen/backend/app/agents/clarify_agent.py#L510) `_generate_kb_queries` | ✅ |
| KB 答案 LLM 校验 | [clarify_agent.py](file:///e:/git-down/ai-test-gen/backend/app/agents/clarify_agent.py#L747) `_verify_kb_answers` | ✅ |
| KB 补全后重新检测 | [clarify_agent.py](file:///e:/git-down/ai-test-gen/backend/app/agents/clarify_agent.py#L208) | ✅ |
| 质量门禁动态阈值 | [quality_gate.py](file:///e:/git-down/ai-test-gen/backend/app/core/quality_gate.py#L90) `_resolve_pass_score` | ✅ |
| API 返回 auto_augmentations | [requirements.py](file:///e:/git-down/ai-test-gen/backend/app/api/requirements.py#L839) | ✅ |
| SRR 模型含 field_conflicts 字段 | [srr.py](file:///e:/git-down/ai-test-gen/backend/app/models/srr.py#L413) | ✅ 模型已定义，但无逻辑填充 |

### 2.3 缺失（本计划需实现）

| 缺失项 | 设计依据 | 当前状态 |
|--------|----------|----------|
| 字段冲突优先级规则 | §4.4.1 `FIELD_PRIORITY` + `_resolve_field_conflict` | `FieldConflict` 模型已存在，但无优先级常量、无冲突解决函数、无来源标记 |
| 需求模式库 | §6.1 `REQUIREMENT_PATTERNS` + §6.2 `RequirementPatternMatcher` | 完全未实现，遗漏检测仅靠 KB 同类需求 |
| 冷启动深度问题 | §4.5 `_generate_cold_start_questions` | 完全未实现，冷启动仅放宽阈值 |
| 前端 KB 补全可视化 | §4.6 交互示例 | API 已返回 `auto_augmentations`，前端未展示 |
| 前端问题分组展示 | §4.5 表格「按优先级分组展示」 | 当前为平铺列表 |
| 前端进度跟踪 | §4.6 交互示例 | 无 answered/total 进度条 |
| 前端单题跳过 | §4.5「允许用户标记跳过」 | 仅有全局「暂不处理」 |
| 前端草稿自动保存 | Trae 提问体验 | 无 localStorage 草稿 |

---

## 三、实施计划

### Phase A：前端澄清交互体验优化（参考 Trae 提问功能）

**目标文件**：
- [frontend/src/pages/RequirementClarify.tsx](file:///e:/git-down/ai-test-gen/frontend/src/pages/RequirementClarify.tsx)（重写，当前 182 行）
- [frontend/src/types/index.ts](file:///e:/git-down/ai-test-gen/frontend/src/types/index.ts)（扩展类型）

**A1. 扩展 ClarifyQuestion 类型**（types/index.ts L50-59）

当前 `ClarifyQuestion` 缺少 `category` 字段（后端已返回）。新增：
```typescript
export interface ClarifyQuestion {
  id: string
  field: string
  question: string
  priority: 'critical' | 'important' | 'optional'
  category?: 'completeness' | 'ambiguity' | 'gap' | 'conflict' | 'cold_start'
  context: string
  default_answer?: string
  input_type?: 'text' | 'select' | 'multi_select'
  options?: string[]
  source?: string  // cold_start_xxx 等
}
```

新增 KB 补全展示类型：
```typescript
export interface KbAugmentation {
  field: string
  value: string
  similarity: number
  source_req_id?: string
}

export interface ClarifyState {
  questions: ClarifyQuestion[]
  previous_answers: Record<string, string>
  quality_report: { overall_score: number; completeness_score?: number; ambiguity_score?: number; summary?: string }
  kb_status: { kb_empty: boolean; product_first_requirement: boolean }
  auto_augmentations: KbAugmentation[]
  field_conflicts?: FieldConflict[]
}

export interface FieldConflict {
  field: string
  existing_value: any
  existing_source: string
  suggested_value: any
  suggested_source: string
  kb_similarity: number
  resolution: 'pending_human' | 'resolved'
}
```

**A2. 重写 RequirementClarify.tsx**（参考 Trae 提问交互）

新版页面结构（自上而下）：

1. **顶部状态栏**
   - 质量评分徽章：`📋 质量评分: 0.65 (❌ 未达标)` + 完整性/歧义性分项
   - KB 状态提示：冷启动模式时显示「❄️ 冷启动模式：知识库为空，阈值已放宽」
   - 进度条：`已回答 3/7` 横向进度条

2. **KB 自动补全展示区**（折叠面板，默认展开）
   - 标题：`🤖 KB 已自动补全 (2 项)`
   - 每项：`✅ preconditions ← REQ-2024-001 (相似度 0.92)`
   - 折叠时显示摘要：`🤖 KB 已自动补全 2 项字段，无需人工回答`
   - **价值**：让用户知道哪些问题 KB 已答，避免重复回答

3. **字段冲突提示区**（仅当 field_conflicts 非空时显示）
   - 标题：`⚠️ 检测到 1 处字段冲突，请确认`
   - 每项：显示 existing_value vs suggested_value，提供「采用原文」「采用 KB」「自定义」三选一
   - **价值**：落地 §4.4.1 冲突处理的人工确认环节

4. **人工澄清问题区**（按优先级分组）
   - 🔴 Critical（必须回答）：红色边框卡片组
   - 🟡 Important（应当回答）：琥珀色边框卡片组
   - 每组标题：`🔴 Critical (2 个问题)` + 折叠/展开
   - **Trae 风格**：每组可整体折叠，Critical 默认展开，Important 可折叠

5. **单题卡片**（Trae 风格优化）
   - 左侧优先级色条（4px 竖条）
   - 问题文本 + context（灰色小字，折叠态）
   - 输入区：select/text/multi_select（保持现有交互）
   - **新增**：右上角「跳过」按钮（标记该题为 skipped，不计入未回答）
   - **新增**：default_answer 一键填充按钮「使用建议答案」
   - **新增**：KB 来源标注（若该问题有 KB 检索记录但未答全，显示「KB 部分命中，相似度 0.72」）

6. **底部操作栏**（sticky）
   - 左：`💾 草稿已自动保存`（localStorage，每 5 秒）
   - 中：进度 `3/7 已回答 · 1 已跳过`
   - 右：「暂不处理」（返回详情）+「提交并重新分析」

**A3. 草稿自动保存**（localStorage）

```typescript
const DRAFT_KEY = `clarify_draft_${id}`
// 每 5 秒或答案变化时保存 { answers, skipped, extraNotes, timestamp }
// 页面加载时：若 previous_answers 为空且 localStorage 有草稿，提示「检测到未提交草稿，是否恢复？」
```

**A4. 单题跳过机制**

```typescript
const [skipped, setSkipped] = useState<Set<string>>(new Set())
// 跳过的题：提交时 answers[qid] = '' 且 skipped.has(qid)
// 进度计算：answered = 非空答案数，skipped = 跳过数，total = 总题数
// 提交时：skipped 的题不阻断提交（但 critical 跳过会提示警告）
```

---

### Phase B：字段冲突优先级规则实现（§4.4.1）

**目标文件**：
- [backend/app/agents/clarify_agent.py](file:///e:/git-down/ai-test-gen/backend/app/agents/clarify_agent.py)（新增冲突解决逻辑）
- [backend/app/models/srr.py](file:///e:/git-down/ai-test-gen/backend/app/models/srr.py)（已定义 FieldConflict，无需改）

**B1. 新增 FIELD_PRIORITY 常量**（clarify_agent.py 顶部）

```python
FIELD_PRIORITY = {
    "USER_INPUT": 0,
    "HUMAN_ANSWERED": 1,
    "KB_INFERRED": 2,
    "PATTERN_DEFAULT": 3,
    "LLM_INFERRED": 4,
}
```

**B2. 新增 `_resolve_field_conflict` 方法**（ClarifyAgent 类内）

按 §4.4.1 决策矩阵实现：
- 字段为空 → 直接填充，标记 `KB_INFERRED`
- 值相同 → 合并来源 `USER_INPUT+KB_CORROBORATED`，提升 confidence
- 值不同 + existing 优先级 ≤ HUMAN_ANSWERED → 不覆盖，记录 `field_conflicts`，resolution=`pending_human`
- 值不同 + existing 是 KB_INFERRED → 相似度更高的胜出
- 值不同 + existing 是 PATTERN_DEFAULT/LLM_INFERRED → KB 覆盖

**B3. 改造 `_apply_kb_answers`**（clarify_agent.py L833）

当前 `_apply_kb_answers` 直接填充空字段。改造为：
1. 读取字段当前值与来源（SRR 中新增 `_field_sources` dict 记录每个字段的来源标记）
2. 调用 `_resolve_field_conflict` 决策
3. 收集 `field_conflicts` 列表
4. 返回 `augmented_srr, augmentations, field_conflicts`

**B4. 来源标记存储**

在 augmented_srr 中新增 `_field_sources: dict[str, str]`（字段路径 → 来源标记），不写入正式 SRR Schema（Pydantic `extra="ignore"` 已处理），仅用于冲突决策。最终 SRR 生成时清除该临时字段。

**B5. field_conflicts 传递到前端**

`clarify()` 返回值新增 `field_conflicts` 字段，API 层 [requirements.py](file:///e:/git-down/ai-test-gen/backend/app/api/requirements.py#L198) `_clarify_state` 新增 `field_conflicts`，前端 A2 步骤展示。

---

### Phase C：需求模式库实现（§6）

**目标文件**：
- 新建 `backend/app/agents/requirement_patterns.py`
- [backend/app/agents/clarify_agent.py](file:///e:/git-down/ai-test-gen/backend/app/agents/clarify_agent.py)（集成模式匹配）

**C1. 新建 requirement_patterns.py**

按 §6.1 定义 `REQUIREMENT_PATTERNS` 字典（auth/crud/payment 三类）+ `RequirementPatternMatcher` 类：

```python
REQUIREMENT_PATTERNS = {
    "auth": {
        "name": "认证授权类",
        "modules": ["auth", "login", "register", "sso"],
        "required_dimensions": [
            "正常登录/注册流程", "异常处理（错误密码、账号不存在）",
            "密码重试锁定机制", "会话管理（Token/Session）", "密码加密存储"
        ],
        "common_boundary": ["用户名长度边界", "密码长度边界", "SQL注入防护"],
        "risk_points": ["暴力破解", "会话固定攻击", "密码泄露"]
    },
    "crud": { ... },  # §6.1 原文
    "payment": { ... },  # §6.1 原文
}

class RequirementPatternMatcher:
    def match_pattern(self, srr: dict) -> dict | None:
        """根据 module 和语义匹配需求模式"""
        # 1. module 精确匹配
        # 2. 标题/摘要关键词匹配（兜底）
        # 3. 返回 {"pattern_id": ..., "required_dimensions": [...], "common_boundary": [...], "risk_points": [...]}
```

**C2. 集成到 ClarifyAgent 遗漏检测**

在 `clarify()` 主流程中，KB 查询前新增模式匹配步骤：
1. `pattern = RequirementPatternMatcher().match_pattern(srr)`
2. 若匹配到模式，将 `required_dimensions` 中当前 SRR 缺失的维度加入 `completeness["important_missing"]`
3. 将 `common_boundary` / `risk_points` 作为 LLM 问题生成的上下文提示（注入 `build_user_prompt` 的 similar_reqs_hint）

**C3. 与测试模式库的边界**（§6.0）

需求模式库仅用于 ClarifyAgent 遗漏检测，不包含测试步骤模板。测试模式库（doc-03 §7.1）保持独立，不在本计划范围。

---

### Phase D：冷启动深度问题实现（§4.5）

**目标文件**：
- [backend/app/agents/clarify_agent.py](file:///e:/git-down/ai-test-gen/backend/app/agents/clarify_agent.py)（新增 `_generate_cold_start_questions`）

**D1. 新增 `_generate_cold_start_questions` 方法**

按 §4.5 设计实现，生成 4 类深度问题：
1. 模块确认（critical）：`这个需求涉及哪些模块？`
2. 影响范围（important）：`这个功能的上线会影响哪些已有功能？`
3. 边界条件（important）：`需求的边界条件是什么？`
4. 依赖确认（optional）：`这个功能依赖哪些外部系统或服务？`

**D2. 集成到 `clarify()` 主流程**

在冷启动模式（`cold_start=True`）下，将冷启动问题合并到最终问题列表：
- 位置：LLM 问题生成之后、`_ensure_critical_questions` 之后
- 合并策略：冷启动问题带 `category="cold_start"` + `source="cold_start_xxx"`，避免与 LLM 问题重复
- 前端可通过 `category="cold_start"` 识别并单独分组展示

---

## 四、假设与决策

### 4.1 关键决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 多轮迭代是否实现 | ❌ 不实现 | SpecFix 2025 刻意简化为单轮，当前单轮已满足需求 |
| 字段来源标记存储位置 | SRR 临时 `_field_sources` | 不污染正式 Schema，`extra="ignore"` 已兼容 |
| 需求模式库存储 | 内存常量（不持久化） | §6.3 持续积累为远期目标，当前仅预定义模式即可用 |
| 冷启动问题是否独立分组 | ✅ 独立分组（category=cold_start） | 便于前端区分展示，避免与 LLM 问题混淆 |
| 草稿保存位置 | localStorage | 无需后端改动，符合 Trae 轻量交互风格 |
| 单题跳过是否阻断提交 | critical 跳过提示警告但不阻断 | 用户可能有「不确定」的合理场景，强制回答反而降低体验 |

### 4.2 可行性保证

1. **不破坏现有链路**：所有后端改动在 `clarify()` 内部增量扩展，API 契约向后兼容（新增字段，不删除现有字段）
2. **前端改动隔离**：RequirementClarify.tsx 重写不影响其他页面，API client 层仅扩展类型
3. **渐进式验证**：每个 Phase 可独立验证，Phase A 不依赖 B/C/D

---

## 五、验证步骤

### Phase A 验证（前端）
1. 上传一个含缺口的需求，进入澄清页
2. 验证顶部质量评分徽章正确显示
3. 验证 KB 补全区展示 auto_augmentations（若有）
4. 验证问题按 Critical/Important 分组
5. 验证单题跳过：跳过 1 题，进度条更新，提交成功
6. 验证草稿：填写 2 题后刷新页面，提示恢复草稿
7. 验证 default_answer 一键填充

### Phase B 验证（字段冲突）
1. 构造场景：KB 有与原文不同的字段值
2. 验证 field_conflicts 出现在 API 返回
3. 验证前端冲突提示区展示
4. 验证用户选择「采用 KB」后，提交时冲突解决

### Phase C 验证（模式库）
1. 上传 auth 类需求（含 login 模块），故意缺失「密码重试锁定」
2. 验证 ClarifyAgent 生成「密码重试锁定机制」相关问题
3. 验证问题带 `category="gap"` 且 context 提及同类需求参考

### Phase D 验证（冷启动）
1. 清空知识库，上传首个需求
2. 验证冷启动深度问题出现（模块/影响/边界/依赖）
3. 验证问题带 `category="cold_start"` + `source="cold_start_xxx"`
4. 验证前端冷启动问题独立分组

### 端到端验证
1. 完整走一遍 7 步工作流：上传 → 看到初稿 + 问题 → KB 补全 → 人工补答 → 重生成 SRR
2. 验证最终 SRR 含 field_conflicts（若有）且来源标记正确

---

## 六、实施顺序

```
Phase A（前端 UX）  ──┐
                      ├── 可并行，互不依赖
Phase B（字段冲突） ──┤
Phase C（模式库）   ──┤
Phase D（冷启动）   ──┘
                      │
                      ▼
              端到端验证
```

建议实施顺序：D → C → B → A（后端先就绪，前端最后对接）。每个 Phase 完成后单独验证，最后端到端联调。

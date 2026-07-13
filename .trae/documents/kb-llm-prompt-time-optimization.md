# 知识库检索 LLM 整合 / KB 专属 LLM 选择 / 时间选择器优化 / 提示词系统修复 - 实施计划

> 本计划基于 Phase 1 代码探索和 Phase 2 用户澄清，针对五项诉求：
> 1. 检查知识库检索功能，确保检索结果通过 LLM 整合后返回更明晰
> 2. 知识库管理页面可选择 LLM，其他调用知识库的功能默认使用该 LLM
> 3. 修复知识库页面和归档时间选择框卡顿问题
> 4. 优化知识库页面归档时间与上线时间的显示（用户反馈两个时间容易混淆）
> 5. 修复提示词系统不一致问题（部分提示词绕过 prompt_manager）

---

## 一、当前状态分析

### 1.1 知识库检索 + LLM 整合 - 已实现，但有优化空间

| 场景 | 当前实现 | 问题 |
|---|---|---|
| 需求分析（ClarifyAgent） | KB 检索结果经 LLM 三轮整合（生成查询/校验答案/精炼问题） | ✅ 已整合 |
| 知识问答（/ask） | kb_sources 组装进 Prompt，LLM 一次性调用生成回答 | ⚠️ QA_SYSTEM_PROMPT 硬编码在代码中，未纳入 prompt_manager 管理 |
| LLM 调用方式 | `/ask` 使用非流式 `llm_client.call()`，结果以单个 chunk 事件发送 | ⚠️ 非真正流式，用户感知"一次性出现" |

### 1.2 KB 专属 LLM 选择 - 完全缺失

- 当前 LLM 是全局配置（Settings 页面），所有功能共用同一个 LLM
- [llm_client.call()](file:///d:/git_down/ai-test-gen/backend/app/core/llm_client.py#L217) 不支持指定 provider，只支持 `model` 参数覆盖
- [_candidate_providers()](file:///d:/git_down/ai-test-gen/backend/app/core/llm_client.py#L338) 以 `settings.LLM_PROVIDER` 为主供应商
- 无 KB 专属 LLM 配置存储

### 1.3 时间选择器卡顿 - 组件重渲染导致

探索发现 onChange 仅更新 state，不触发 API。卡顿原因是：
- [KnowledgeBase.tsx](file:///d:/git_down/ai-test-gen/frontend/src/pages/KnowledgeBase.tsx) 中整个列表和弹窗在同一个组件中
- 当 entries 列表较大时，datetime-local input 的每次 onChange 都触发整个组件重渲染（包括所有列表项）
- [RequirementDetail.tsx](file:///d:/git_down/ai-test-gen/frontend/src/pages/RequirementDetail.tsx) 归档弹窗同理，页面内容复杂时重渲染开销大

### 1.4 归档时间 + 上线时间显示 - 用户反馈易混淆

[KnowledgeBase.tsx L419-432](file:///d:/git_down/ai-test-gen/frontend/src/pages/KnowledgeBase.tsx#L419) 中同一行显示：
```
产品名 · 🕐 归档时间 · 📅 上线时间 · 摘要
```
用户反馈两个时间显示在一起容易混淆，希望区分更清晰。

### 1.5 提示词系统 - 机制正确，但有 3 处不一致

确认机制：**代码中固定默认值 → DB 存储覆盖值 → DB 优先 → 重置时用 DB 中的 default_value 覆盖 value**。但有以下不一致：

| Agent | 提示词 | 当前调用方式 | 问题 |
|---|---|---|---|
| gen_agent.py L105 | SYSTEM_PROMPT | `prompts.SYSTEM_PROMPT` 直接引用 | ❌ 未通过 prompt_manager，无法被 DB 覆盖 |
| clarify_agent.py L605 | KB_QUERY_GENERATOR_SYSTEM_PROMPT | `prompts.KB_QUERY_GENERATOR_SYSTEM_PROMPT` 直接引用 | ❌ 未通过 prompt_manager |
| clarify_agent.py L974 | KB_ANSWER_VERIFIER_SYSTEM_PROMPT | `prompts.KB_ANSWER_VERIFIER_SYSTEM_PROMPT` 直接引用 | ❌ 未通过 prompt_manager |
| knowledge_qa.py L50 | QA_SYSTEM_PROMPT | 代码内硬编码 | ❌ 未纳入 prompt_manager，未注册到 registry |

---

## 二、拟议变更

### 变更组 A：llm_client 支持 provider 参数（后端）

**文件**: [backend/app/core/llm_client.py](file:///d:/git_down/ai-test-gen/backend/app/core/llm_client.py)

**变更内容**：
1. `call()` 方法新增 `provider: str | None = None` 参数
2. `call_json()` 方法同步新增 `provider` 参数并透传
3. `_candidate_providers()` 方法新增 `provider: str | None = None` 参数：
   - 若指定 provider，仅从 `all_configs` 中取该 provider 的配置（跳过主供应商优先逻辑）
   - 若该 provider 不可用（被禁用或无 key），回退到正常候选列表

```python
async def call(self, prompt, system_prompt=None, temperature=0.3,
               response_format=None, model=None, max_tokens=None,
               provider: str | None = None) -> str:
    # ...
    providers = self._candidate_providers(model, provider=provider)
    # ...

def _candidate_providers(self, model, provider: str | None = None) -> list[dict]:
    # 若指定 provider，仅返回该 provider 的配置
    if provider and all_configs and provider in all_configs:
        cfg = all_configs[provider]
        if cfg.get("api_key"):
            key = f"{provider}:{cfg.get('model', '')}"
            if self._provider_disabled_until.get(key, 0) < now:
                return [{
                    "key": key, "api_key": cfg["api_key"],
                    "base_url": cfg.get("base_url", ""),
                    "model": model or cfg.get("model", ""),
                }]
    # 回退到正常逻辑
    # ...
```

**Why**: 让知识问答等场景可以指定使用 KB 专属 LLM，而非全局主 LLM。

---

### 变更组 B：KB 专属 LLM 配置存储与 API（后端）

**文件 1**: [backend/app/db/models.py](file:///d:/git_down/ai-test-gen/backend/app/db/models.py)

**变更内容**：新增 KB 设置模型（或复用 key-value 方式）：
```python
class KBSetting(Base):
    """知识库专属设置（如 KB 问答使用的 LLM 供应商）。"""
    __tablename__ = "kb_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
```

**文件 2**: [backend/app/db/database.py](file:///d:/git_down/ai-test-gen/backend/app/db/database.py)

**变更内容**：在 `init_db` 的 `Base.metadata.create_all` 中自动创建 `kb_settings` 表（无需额外迁移，新表自动创建）。

**文件 3**: [backend/app/api/knowledge.py](file:///d:/git_down/ai-test-gen/backend/app/api/knowledge.py)

**变更内容**：新增两个端点：
```python
@router.get("/kb-llm", response_model=ApiResponse, dependencies=[Depends(get_current_user)])
async def get_kb_llm(session: AsyncSession = Depends(get_session)):
    """获取知识库专属 LLM 配置。"""
    # 读取 kb_settings 中 key='llm_provider' 的值
    # 同时返回可用供应商列表（从 LLMConfig 表）
    # 返回: { provider: "deepseek", providers: [{provider, provider_label, model}, ...] }

@router.put("/kb-llm", response_model=ApiResponse, dependencies=[Depends(require_admin)])
async def set_kb_llm(body: dict, session: AsyncSession = Depends(get_session)):
    """设置知识库专属 LLM 供应商。"""
    # body: { provider: "deepseek" }
    # 写入 kb_settings 表
```

**文件 4**: [backend/app/api/knowledge_qa.py](file:///d:/git_down/ai-test-gen/backend/app/api/knowledge_qa.py)

**变更内容**：`/ask` 端点（kqa_ask）中读取 KB LLM 配置，传给 `llm_client.call()`：
```python
# 读取 KB 专属 LLM
kb_provider = await _get_kb_llm_provider(session)
# 在 _stream_qa_answer 中使用
answer = await llm_client.call(
    prompt=prompt, system_prompt=sys_prompt,
    temperature=0.3, max_tokens=4096,
    provider=kb_provider,  # 新增
)
```

**文件 5**: [backend/app/agents/clarify_agent.py](file:///d:/git_down/ai-test-gen/backend/app/agents/clarify_agent.py)

**变更内容**：ClarifyAgent 的 KB 相关 LLM 调用（`_generate_kb_queries`、`_verify_kb_answers`）也使用 KB 专属 LLM。需将 provider 传入这些方法。

**Why**: 用户明确要求"知识库管理页面选择 LLM，其他调用知识库的默认使用该 LLM"。

---

### 变更组 C：QA_SYSTEM_PROMPT 纳入 prompt_manager（后端）

**文件 1**: [backend/app/prompts/knowledge_qa.py](file:///d:/git_down/ai-test-gen/backend/app/prompts/knowledge_qa.py)（新建）

**变更内容**：将 [knowledge_qa.py L50-60](file:///d:/git_down/ai-test-gen/backend/app/api/knowledge_qa.py#L50) 中的 `QA_SYSTEM_PROMPT` 移到此文件，并优化提示词使回答更明晰：
```python
QA_SYSTEM_PROMPT = """你是 AI-Test-Gen 系统的知识问答助手，专注于软件测试领域。

你的任务是基于知识库中的需求文档和测试用例来回答用户的问题。

请严格遵循以下规则：
1. 只能基于提供的知识库上下文回答问题，不要编造信息
2. 如果知识库中没有相关信息，请明确告知用户"知识库中未找到相关内容"
3. 引用来源时，使用「来源类型: 来源标题」的格式，禁止编造任何 ID 或编号
4. 回答要结构清晰、准确专业，使用 Markdown 格式
5. 如果检索到多个版本的需求，以"上线日期"靠后的最新版本为准，并注明参考版本
6. 如果上下文不足以完整回答，基于已有信息给出最佳推断，并说明信息局限性
7. 对于技术细节问题，给出完整的分析和推理过程"""
```

**文件 2**: [backend/app/core/prompt_manager.py](file:///d:/git_down/ai-test-gen/backend/app/core/prompt_manager.py)

**变更内容**：
1. import 添加 `knowledge_qa`
2. `_PROMPT_REGISTRY` 新增注册项：
```python
{
    "module": "knowledge_qa",
    "name": "QA_SYSTEM_PROMPT",
    "default": knowledge_qa.QA_SYSTEM_PROMPT,
    "description": "知识问答系统指令 - 基于知识库上下文回答用户问题",
},
```

**文件 3**: [backend/app/api/knowledge_qa.py](file:///d:/git_down/ai-test-gen/backend/app/api/knowledge_qa.py)

**变更内容**：
- 删除内联的 `QA_SYSTEM_PROMPT` 定义
- 改为 `from app.core.prompt_manager import prompt_manager`
- 使用 `sys_prompt = prompt_manager.get("knowledge_qa", "QA_SYSTEM_PROMPT")`

**Why**: QA_SYSTEM_PROMPT 应可通过管理后台编辑，与其他 Agent 提示词一致。

---

### 变更组 D：修复提示词系统不一致（后端）

**文件 1**: [backend/app/agents/gen_agent.py](file:///d:/git_down/ai-test-gen/backend/app/agents/gen_agent.py#L105)

**变更内容**：将 `prompts.SYSTEM_PROMPT` 改为 `prompt_manager.get("gen_agent", "SYSTEM_PROMPT")`：
```python
# 原: system_prompt=prompts.SYSTEM_PROMPT,
# 改:
from app.core.prompt_manager import prompt_manager
system_prompt=prompt_manager.get("gen_agent", "SYSTEM_PROMPT"),
```

**文件 2**: [backend/app/agents/clarify_agent.py](file:///d:/git_down/ai-test-gen/backend/app/agents/clarify_agent.py#L605)

**变更内容**：将 `prompts.KB_QUERY_GENERATOR_SYSTEM_PROMPT` 改为 `prompt_manager.get("clarify_agent", "KB_QUERY_GENERATOR_SYSTEM_PROMPT")`

**文件 3**: [backend/app/agents/clarify_agent.py](file:///d:/git_down/ai-test-gen/backend/app/agents/clarify_agent.py#L974)

**变更内容**：将 `prompts.KB_ANSWER_VERIFIER_SYSTEM_PROMPT` 改为 `prompt_manager.get("clarify_agent", "KB_ANSWER_VERIFIER_SYSTEM_PROMPT")`

**文件 4**: [backend/app/core/prompt_manager.py](file:///d:/git_down/ai-test-gen/backend/app/core/prompt_manager.py)

**变更内容**：`_PROMPT_REGISTRY` 新增两个注册项：
```python
{
    "module": "clarify_agent",
    "name": "KB_QUERY_GENERATOR_SYSTEM_PROMPT",
    "default": clarify_agent.KB_QUERY_GENERATOR_SYSTEM_PROMPT,
    "description": "ClarifyAgent KB 查询生成器 - LLM 动态生成上下文感知的搜索查询",
},
{
    "module": "clarify_agent",
    "name": "KB_ANSWER_VERIFIER_SYSTEM_PROMPT",
    "default": clarify_agent.KB_ANSWER_VERIFIER_SYSTEM_PROMPT,
    "description": "ClarifyAgent KB 答案校验器 - LLM 审核检索结果的相关性与准确性",
},
```

**Why**: 这 3 个提示词当前直接引用代码常量，无法通过管理后台编辑/覆盖，与系统设计不一致。

---

### 变更组 E：前端时间选择器性能优化

**文件 1**: [frontend/src/pages/KnowledgeBase.tsx](file:///d:/git_down/ai-test-gen/frontend/src/pages/KnowledgeBase.tsx)

**变更内容**：将"修改上线时间"和"修改归档时间"弹窗提取为独立组件，用 `React.memo` 包裹，避免列表项重渲染影响弹窗内 input 性能：

```tsx
// 提取为独立组件
const ArchiveTimeEditModal = React.memo(function ArchiveTimeEditModal({
  entry, editValue, onChange, onSave, onCancel, saving,
}: { ... }) {
  return (
    <div className="fixed inset-0 ...">
      {/* 弹窗内容 */}
    </div>
  )
})
```

同理处理 `GoLiveEditModal`。

在主组件中使用：
```tsx
{editingArchiveEntry && (
  <ArchiveTimeEditModal
    entry={editingArchiveEntry}
    editValue={editArchiveTime}
    onChange={setEditArchiveTime}
    onSave={handleSaveArchiveTime}
    onCancel={() => { setEditingArchiveEntry(null); setEditArchiveTime('') }}
    saving={savingArchiveTime}
  />
)}
```

**文件 2**: [frontend/src/pages/RequirementDetail.tsx](file:///d:/git_down/ai-test-gen/frontend/src/pages/RequirementDetail.tsx#L1199)

**变更内容**：将归档弹窗提取为 `React.memo` 组件，避免页面复杂内容导致的重渲染。

**Why**: datetime-local / date input 的 onChange 触发 setState，导致父组件重渲染。当列表/页面内容多时，重渲染开销大，表现为卡顿。提取为 memo 组件后，onChange 只重渲染弹窗自身。

---

### 变更组 F：归档时间与上线时间显示优化

**文件**: [frontend/src/pages/KnowledgeBase.tsx](file:///d:/git_down/ai-test-gen/frontend/src/pages/KnowledgeBase.tsx#L419)

**变更内容**：将归档时间和上线时间分行显示，增加视觉区分度：

```tsx
// 原来全部在一行:
// 产品名 · 🕐 归档时间 · 📅 上线时间 · 摘要

// 改为两行:
<p className="text-xs text-slate-400 mt-0.5 truncate flex items-center gap-1">
  {entry.product_id && (products.find(p => p.id === entry.product_id)?.name || entry.product_id)}
  {entry.archived_at && (
    <span className="flex items-center gap-0.5">
      · <Clock className="w-2.5 h-2.5" /> 归档于 {formatDate(entry.archived_at)}
    </span>
  )}
  {entry.summary ? ` · ${entry.summary}` : ''}
</p>
{entry.go_live_date && (
  <p className="text-xs text-primary-500 mt-0.5 flex items-center gap-0.5">
    <Calendar className="w-2.5 h-2.5" /> 上线时间: {formatDateTime(entry.go_live_date)}
  </p>
)}
```

**Why**: 用户反馈两个时间在同一行容易混淆，分行后上线时间用 primary 色突出，视觉上更清晰。

---

### 变更组 G：前端 KnowledgeBase 页面新增 LLM 选择

**文件 1**: [frontend/src/api/client.ts](file:///d:/git_down/ai-test-gen/frontend/src/api/client.ts)

**变更内容**：新增 KB LLM 配置 API：
```typescript
// 在 knowledge 对象中新增
getKbLlm: () => request<{ provider: string; providers: Array<{provider: string; provider_label: string; model: string}> }>(
  '/knowledge/kb-llm'
),
setKbLlm: (provider: string) =>
  request<{ provider: string }>('/knowledge/kb-llm', {
    method: 'PUT', body: JSON.stringify({ provider }),
  }),
```

**文件 2**: [frontend/src/pages/KnowledgeBase.tsx](file:///d:/git_down/ai-test-gen/frontend/src/pages/KnowledgeBase.tsx)

**变更内容**：
1. 新增 state：`kbLlmProvider: string` 和 `llmProviders: Array<{...}>`
2. 页面加载时调用 `knowledge.getKbLlm()` 获取当前 KB LLM 配置和可用供应商列表
3. 在页面头部（"知识库"标题下方）新增 LLM 选择下拉框：
```tsx
<div className="flex items-center gap-3">
  <span className="text-sm text-slate-500">知识库 LLM：</span>
  <select
    value={kbLlmProvider}
    onChange={e => handleSetKbLlm(e.target.value)}
    className="select-field text-sm max-w-[200px]"
  >
    {llmProviders.map(p => (
      <option key={p.provider} value={p.provider}>
        {p.provider_label} ({p.model})
      </option>
    ))}
  </select>
</div>
```
4. `handleSetKbLlm` 调用 `knowledge.setKbLlm(provider)` 并更新本地 state

**Why**: 用户明确要求在知识库管理页面选择 LLM。

---

## 三、假设与决策

### 假设
1. **KB LLM 配置存储**：使用独立的 `kb_settings` 表（key-value 方式），而非在 LLMConfig 表中增加字段。这样可扩展其他 KB 设置。
2. **provider 回退**：若 KB 指定的 LLM provider 不可用（未配置 key 或被断路器禁用），回退到全局主 LLM。
3. **ClarifyAgent 的 KB LLM**：ClarifyAgent 的 KB 相关 LLM 调用也使用 KB 专属 LLM，因为它们都是"调用知识库"的场景。
4. **时间选择器卡顿**：根因是组件重渲染而非 input 本身，React.memo 隔离是正确方案。

### 决策
- **KB LLM 影响范围**：knowledge_qa /ask 的 LLM 调用 + ClarifyAgent 的 KB 查询生成和答案校验。不影响 ReqAgent 解析、GenAgent 生成、ReviewAgent 审核（这些不是"调用知识库"）。
- **归档时间显示**：分行显示，上线时间用 primary 色突出。
- **QA_SYSTEM_PROMPT**：优化提示词内容（增加 Markdown 格式要求、版本说明要求），并纳入 prompt_manager。

---

## 四、实施顺序

1. **变更组 A**（llm_client.py）-> call/call_json/_candidate_providers 支持 provider 参数
2. **变更组 B**（models.py + database.py + knowledge.py + knowledge_qa.py + clarify_agent.py）-> KB LLM 配置存储与使用
3. **变更组 C**（prompts/knowledge_qa.py + prompt_manager.py + knowledge_qa.py）-> QA_SYSTEM_PROMPT 纳入管理
4. **变更组 D**（gen_agent.py + clarify_agent.py + prompt_manager.py）-> 修复提示词不一致
5. **变更组 E**（KnowledgeBase.tsx + RequirementDetail.tsx）-> 时间选择器性能优化
6. **变更组 F**（KnowledgeBase.tsx）-> 归档时间显示优化
7. **变更组 G**（client.ts + KnowledgeBase.tsx）-> 前端 KB LLM 选择

---

## 五、验证步骤

### 5.1 后端验证
1. **llm_client provider 参数**：调用 `llm_client.call(prompt, provider="openai")` 验证使用指定 provider
2. **KB LLM 端点**：`GET /api/knowledge/kb-llm` 返回当前 provider + 可用列表；`PUT /api/knowledge/kb-llm` 更新成功
3. **/ask 使用 KB LLM**：设置 KB LLM 为 openai 后，/ask 调用日志显示使用 openai provider
4. **提示词覆盖**：通过管理后台修改 QA_SYSTEM_PROMPT，/ask 回答风格变化
5. **gen_agent prompt_manager**：修改 gen_agent SYSTEM_PROMPT 后，生成用例行为变化
6. **提示词重置**：重置后 value == default_value

### 5.2 前端验证
1. **KB LLM 选择**：KnowledgeBase 页面下拉框显示可用供应商，切换后持久化
2. **时间选择器不卡顿**：弹窗中 datetime-local input 输入流畅，无明显延迟
3. **归档时间显示**：归档时间和上线时间分行显示，视觉清晰

### 5.3 提示词系统验证
1. `prompt_manager.list_prompts()` 包含 knowledge_qa.QA_SYSTEM_PROMPT、clarify_agent.KB_QUERY_GENERATOR_SYSTEM_PROMPT、clarify_agent.KB_ANSWER_VERIFIER_SYSTEM_PROMPT
2. 管理后台可编辑这 3 个新注册的提示词
3. gen_agent.py 不再直接引用 `prompts.SYSTEM_PROMPT`

---

## 六、风险与回滚

- 每个变更组独立提交，可单独 revert
- KB LLM 配置为可选功能，不设置时回退到全局主 LLM
- React.memo 优化不改变功能逻辑，仅影响渲染性能
- 提示词注册新增项不影响已有提示词

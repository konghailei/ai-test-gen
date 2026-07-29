# 01 — 需求解析与格式化

> 原始需求（支持多文档输入） → SRR JSON → 需求澄清 → 质量门禁 → 放行进入生成流程

---

## 1. 设计目标

软件需求天然存在多种形态：PRD、JIRA Ticket、用户故事、接口文档。AI 直接消费时存在三大顽疾：

| 问题 | 表现 | 根因 |
|------|------|------|
| **歧义性** | "用户登录失败应有提示"——提示什么？何时触发？ | 自然语言的模糊性 |
| **遗漏** | AI 可能忽略隐含的前置条件、边界值和异常路径 | 需求隐式知识的缺失 |
| **不一致** | 同一功能在多处描述不一致时，AI 无法判断优先级 | 缺乏权威性标注 |

**解决方案**：建立结构化需求中间表示 (SRR) + DocMergeAgent 多文档融合 + ClarifyAgent 需求澄清闭环 + QualityGate 质量门禁，**确保需求在进入用例生成前是明确、完整、无歧义的**。

---

## 2. 需求结构化：三级递进模型

```
Level 0: 原始需求文本
     │ (ReqAgent 解析)
     ▼
Level 1: 标准化需求模板 (Markdown)
     │ (关键要素提取 + 实体识别)
     ▼
Level 2: 结构化需求 JSON (SRR)
     │ (ClarifyAgent 澄清 + QualityGate 门禁)
     ▼
Level 3: 细化后的 SRR v2 (进入生成流程)
```

### 2.1 Level 1: 标准化需求模板 (Markdown)

```markdown
# [需求ID] 需求标题

## 元信息
- **需求ID**: REQ-2024-001
- **产品ID**: PROD-001
- **产品名称**: 用户中心
- **来源**: JIRA / PRD / 用户故事
- **优先级**: P0 / P1 / P2 / P3
- **关联模块**: [模块A, 模块B]（支持多模块）
- **版本**: v1.0

## 功能概述
[一句话描述核心功能]

## 前置条件
1. [条件1]

## 正常流程 (Happy Path)
1. [步骤1] → [预期结果]

## 异常流程
- [异常场景A]: [处理逻辑]

## 边界条件
- [边界值1]: [预期行为]

## 业务规则
- [规则1]: [描述 + 约束]

## 数据约束
| 字段 | 类型 | 必填 | 约束 | 示例 |

## 非功能需求
- 性能: [响应时间]
- 安全: [权限/加密]

## 关联需求
- [REQ-xxx] 依赖/被依赖
```

### 2.2 Level 2: 结构化需求 JSON (SRR v2.1) ⭐

```json
{
  "srr_version": "1.0",
  "requirement": {
    "id": "REQ-2024-001",
    "product_id": "PROD-001",
    "product_name": "用户中心",
    "title": "用户登录功能",
    "priority": "P0",
    "modules": ["auth", "session"],
    "summary": "用户通过账号密码登录系统，登录成功后跳转首页。支持验证码校验，连续失败5次锁定30分钟。"
  },
  "preconditions": ["用户已在系统注册", "用户未处于锁定状态"],
  "flows": {
    "happy_path": {
      "steps": [
        {"order": 1, "action": "打开登录页", "expect": "显示登录表单"},
        {"order": 2, "action": "输入有效账号密码", "expect": "输入框正常"},
        {"order": 3, "action": "点击登录按钮", "expect": "登录成功，跳转首页"}
      ]
    },
    "alternate_flows": [
      {"name": "账号不存在", "trigger": "输入未注册账号", "expect": "提示「账号不存在」"},
      {"name": "密码错误", "trigger": "输入错误密码", "expect": "提示「密码错误」"}
    ]
  },
  "boundary_conditions": [
    {"field": "密码", "condition": "长度 < 6", "expect": "前端校验拦截"},
    {"field": "密码", "condition": "长度 = 6", "expect": "允许提交"}
  ],
  "business_rules": [
    {"id": "BR-01", "rule": "连续失败5次锁定30分钟", "type": "security"}
  ],
  "data_constraints": [
    {"field": "username", "type": "string", "required": true, "constraints": "3-50 chars", "example": "john_doe"},
    {"field": "password", "type": "string", "required": true, "constraints": "6-128 chars", "example": "***"}
  ],
  "non_functional": {
    "performance": "P99 < 500ms",
    "security": "TLS 1.3, bcrypt加密"
  },
  "relationships": [
    {"target": "REQ-2024-000", "type": "depends_on"}
  ],
  "test_points_hint": [
    {"category": "functional", "point": "正常登录流程"},
    {"category": "boundary", "point": "密码长度边界值"}
  ],
  // ===== v2.1 新增字段（完整映射需求原文） =====
  "acceptance_criteria": [
    {"id": "AC-01", "description": "用户可凭正确账号密码登录", "type": "functional"},
    {"id": "AC-02", "description": "错误密码提示且记录失败次数", "type": "functional"},
    {"id": "AC-03", "description": "连续失败5次锁定30分钟", "type": "security"}
  ],
  "api_definitions": [
    {"method": "POST", "path": "/api/v1/auth/login", "summary": "用户登录",
     "request_body": "{\"username\": string, \"password\": string}",
     "response_body": "{\"token\": string, \"expires_in\": number}",
     "error_responses": ["401 账号或密码错误", "423 账号已锁定"]}
  ],
  "user_roles": [
    {"role": "普通用户", "description": "拥有登录权限", "permissions": ["login"]},
    {"role": "管理员", "description": "拥有全部管理权限", "permissions": ["login", "admin"]}
  ],
  "dependencies": [
    {"name": "用户数据库", "type": "DB", "description": "存储用户账号密码信息",
     "impact_if_unavailable": "登录功能完全不可用"}
  ],
  "ui_rules": [
    {"element": "登录按钮", "behavior": "频繁点击限流，60秒内最多点击10次", "condition": "点击频率 > 10次/分钟"}
  ],
  "data_model": [
    {"entity_name": "User", "description": "用户实体",
     "fields": [
       {"field": "username", "type": "string", "required": true, "constraints": "3-50字符", "example": "john"},
       {"field": "password", "type": "string", "required": true, "constraints": "bcrypt加密", "example": "***"}
     ],
     "relationships": ["User belongs_to Role"]}
  ],
  "assumptions": [
    {"content": "用户已在系统中注册", "type": "assumption"},
    {"content": "密码通过HTTPS传输", "type": "constraint"},
    {"content": "第三方OAuth登录不在本次范围", "type": "out_of_scope"},
    {"content": "本次登录功能仅支持账号密码方式，后续版本将支持手机验证码登录", "type": "out_of_scope"}
  ]
}
```

### 2.2.1 v2.1 新增字段说明

> **设计动机**：原有 12 个标准字段无法完整覆盖需求文档中的常见内容（如验收标准、API定义、用户角色等），导致解析结果信息丢失。v2.1 新增 8 个可选字段，确保原文信息可被完整映射。

| 字段 | 用途 | 对应文档常见内容 |
|------|------|----------------|
| `acceptance_criteria` | 验收标准/验收条件 | 敏捷PRD中的"验收条件"、"Definition of Done" |
| `api_definitions` | API接口定义 | 接口文档中的端点、请求/响应结构 |
| `user_roles` | 用户角色与权限 | 需求中的"谁可以使用"、"权限控制" |
| `dependencies` | 外部依赖 | 第三方服务、数据库、消息队列等外部系统 |
| `ui_rules` | 界面/交互规则 | 前端行为描述、交互逻辑、UI状态变化 |
| `data_model` | 数据模型/实体关系 | 数据表结构、实体关系图、字段定义 |
| `assumptions` | 假设与约束 | 业务假设、技术约束、明确排除的范围 |

**完整性策略**：上述字段均为可选（LLM 按原文内容决定是否输出）。v2.40 起移除了原 `additional_details` 兜底字段——所有原文内容必须归入上述标准字段之一（无法归类的内容归入 `assumptions` 并标注 `type`），避免 LLM 偷懒把整段原文塞入兜底字段导致下游 Agent（Gen/Review/Impact）无法消费。

### 2.2.2 SRR 校验容错机制（v2.1 新增）⭐

> **问题**：LLM 输出的 JSON 字段名和结构与 Pydantic Schema 不可能完全对齐。LLM 可能输出 `"name"` 而非 `"field"`、`"expected"` 而非 `"expect"`、漏掉某些非关键字段、或添加 Schema 不认识的字段。过去严格校验会导致整个解析失败。

**三层容错机制**：

```
LLM 原始输出
    │
    ▼
┌─── 第 1 层：字段名归一化 ─────────────────────┐
│   name → field, expected → expect,            │
│   endpoint → path, nullable → required, ...   │
│   （40+ 条映射规则，见 _normalize_field_names） │
└──────────────────┬───────────────────────────┘
                   ▼
┌─── 第 2 层：Literal 值映射 ────────────────────┐
│   business_rules.type: session → security      │
│   acceptance_criteria.type: custom → functional│
└──────────────────┬───────────────────────────┘
                   ▼
┌─── 第 3 层：宽松 Schema 校验 ───────────────────┐
│   ├── Critical 字段（title/summary/happy_path）→ 阻断
│   ├── 其他字段 → try/except 逐个处理，失败只记 warning
│   └── 所有子模型 model_config.extra = "ignore"
└──────────────────┬───────────────────────────┘
                   ▼
          后续流程正常流转 ✅
```

**实现细节**：

| 机制 | 所在代码 | 效果 |
|------|---------|------|
| `model_config = ConfigDict(extra="ignore")` | 所有 20 个子模型 | LLM 输出的多余字段被静默忽略，不报错 |
| 必填字段改为带默认值 | AlternateFlow.name, DataConstraint.field, ... | 即使 LLM 漏掉也能用空字符串兜底 |
| `_validate_lenient()` | ReqAgent._validate_lenient | 只对 Critical 字段做严格检查，其余容错 |
| `_remap_dict_keys()` | ReqAgent._remap_dict_keys | 40+ 条别名→正式名映射 |

**设计理念**：LLM 的 JSON 输出本质上是"近似正确"的，不应该用严格 Schema 去套。采用**"宽松接收 + 智能归一 + 关键字段保障"**策略，既保证数据质量，又不让 LLM 的小偏差阻塞业务流程。

### 2.3 Level 3: 向量化

将 SRR 的 `title + summary + flows + business_rules` 拼接为语义文本，通过 Embedding 模型生成向量，存入向量数据库用于 MatchAgent 语义检索。

---

## 3. ReqAgent 解析流程

```
原始需求输入
     │
     ▼
① 格式自动检测 (Markdown/JSON/JIRA/纯文本)
     ▼
② 预处理 (去噪、分段、语言检测)
     ▼
③ LLM 结构化解析 (Prompt: 填充 SRR JSON，CoT 引导分步提取)
     ▼
④ 字段校验 (Schema校验、必填检查)
     ▼
⑤ 实体链接 (关联已有需求ID、模块)
     ▼
⑥ 测试点提示生成 (LLM 推断 test_points_hint)
     ▼
  SRR JSON 输出
     │
     ▼  ← 不直接进入生成，先经过 ClarifyAgent
  ClarifyAgent 澄清 (§4)
     ▼
  QualityGate 门禁 (§5)
     ▼
  ✅ 细化后的 SRR v2 → 进入知识库检索 + 用例生成
```

### 3.1 ReqAgent 核心 Prompt

```
系统指令:
你是一个专业的软件需求分析师。请将以下需求文本解析为结构化的 JSON 格式。
严格遵循给定的 JSON Schema，不要遗漏任何字段。

解析策略（按顺序执行）:
1. 识别功能概述——用一句话说清楚这个需求做什么
2. 提取前置条件——完成此功能需要满足什么
3. 描述正常流程——按步骤列出 happy path
4. 枚举异常场景——思考所有可能的错误/异常情况
5. 识别边界条件——输入/输出的临界值
6. 提取业务规则——计算公式、约束条件
7. 整理数据约束——字段、类型、校验规则
8. 标注非功能需求——性能、安全、兼容性

注意:
- 如果需求原文未提及某字段，标注为 null，不要编造
- 异常场景需要包含"触发条件"和"预期结果"
- test_points_hint 是推断的测试关注点，不是最终测试用例
```

---

## 4. ClarifyAgent 需求澄清闭环

### 4.1 设计理念

借鉴 ClarifyGPT (ACL 2024)：**人类开发者在面对模糊需求时会主动提问，LLM 也应如此**。

核心原则：
1. **仅对模糊需求提问** — 不浪费用户时间
2. **KB 优先** — 能自动补全的不问人工
3. **问题精准** — 基于差异分析，不问无关问题
4. **多轮迭代** — 最多 3 轮，质量达标才放行

### 4.2 工作流程

```
SRR JSON
    │
    ▼
① 完整性检测 (Completeness)
   基于 Checklist 规则检查必填字段
   分级: Critical / Important / Optional
    │
    ▼
② 歧义性检测 (Ambiguity)
   规则检测 (模糊量词/条件) + LLM 语义分析 (矛盾/逻辑断裂)
    │
    ▼
③ 遗漏检测 (Gap Detection)
   对比需求模式库，发现同类需求普遍有但当前缺失的维度
    │
    ▼
④ KB 自动补全
   对每个缺口检索知识库 → 相似度 > 0.85 → 自动填充
   标记 [KB_INFERRED]，供人工确认
    │
    ├─ KB 命中 → 自动补全
    │
    └─ KB 未命中 ↓
⑤ 问题生成
   只针对 KB 无法解决的问题生成针对性问题
   按优先级排序: Critical → Important → Optional
    │
    ▼
⑥ 人工回答
   用户提供答案 (支持批量回答)
    │
    ▼
⑦ 需求细化
   合并 KB 补全 + 人工回答 → SRR v2
    │
    ▼
⑧ 质量评分
   达标 → 放行
   不达标 → 回到 ② (最多 3 轮)
```

### 4.3 三级检测机制

#### 4.3.1 完整性检测

```python
# 必填字段清单（按测试影响分级）
COMPLETENESS_CHECKLIST = {
    "critical": {  # 🔴 缺失则无法生成有效用例
        "summary": "功能概述",
        "flows.happy_path": "正常流程（至少3个步骤）",
        "data_constraints": "数据约束（字段定义）",
    },
    "important": {  # 🟡 缺失则用例质量下降
        "preconditions": "前置条件",
        "flows.alternate_flows": "异常流程",
        "boundary_conditions": "边界条件",
        "business_rules": "业务规则",
    },
    "optional": {  # 🟢 可通过 KB 补全
        "non_functional.performance": "性能需求",
        "non_functional.security": "安全需求",
        "relationships": "关联需求",
    }
}

def check_completeness(srr: dict) -> dict:
    """检查 SRR 完整性，返回缺失字段列表和完整性评分"""
    result = {"critical_missing": [], "important_missing": [], "optional_missing": []}
    for level, fields in COMPLETENESS_CHECKLIST.items():
        for field, desc in fields.items():
            if not get_nested_value(srr, field):
                result[f"{level}_missing"].append({"field": field, "description": desc})
    # 评分: critical缺失×3 + important缺失×1 + optional缺失×0.3
    missing_weight = (len(result["critical_missing"]) * 3 +
                      len(result["important_missing"]) * 1 +
                      len(result["optional_missing"]) * 0.3)
    total = sum(len(v) for v in COMPLETENESS_CHECKLIST.values())
    result["completeness_score"] = max(0, 1 - missing_weight / total)
    result["passed"] = len(result["critical_missing"]) == 0
    return result
```

#### 4.3.2 歧义性检测

```python
AMBIGUITY_PATTERNS = {
    "vague_quantifier": {
        "patterns": ["一些", "若干", "适当", "合理", "多个", "大量", "较快", "较慢"],
        "question_template": "「{original}」中的「{keyword}」具体指什么？请提供明确数值或范围。"
    },
    "vague_condition": {
        "patterns": ["必要时", "根据情况", "视情况而定", "如果需要", "特殊情况下"],
        "question_template": "「{original}」中「{keyword}」的具体触发条件是什么？请列举所有情况。"
    },
    # 矛盾描述、未定义术语、缺少预期结果 → 需 LLM 语义分析
}

async def detect_ambiguity(srr: dict, llm_client) -> dict:
    """规则检测（快速）+ LLM 检测（深度）"""
    ambiguities = []
    # 1. 基于规则: 扫描模糊量词/条件
    for amb_type, config in AMBIGUITY_PATTERNS.items():
        for pattern in config["patterns"]:
            contexts = find_contexts(srr, pattern)
            for ctx in contexts:
                ambiguities.append({
                    "type": amb_type, "location": ctx["location"],
                    "original_text": ctx["text"], "keyword": pattern,
                    "suggested_question": config["question_template"].format(
                        original=ctx["text"], keyword=pattern)
                })
    # 2. 基于 LLM: 检测矛盾、逻辑断裂、缺少预期
    llm_ambiguities = await llm_detect(srr, llm_client)
    ambiguities.extend(llm_ambiguities)
    # 评分: 每个歧义 +0.15
    score = min(1.0, len(ambiguities) * 0.15)
    return {"ambiguities": ambiguities, "ambiguity_score": score, "passed": score < 0.3}
```

#### 4.3.3 遗漏检测（基于需求模式库）

```python
async def detect_gaps(srr: dict, knowledge_base) -> dict:
    """
    对比同类需求模式，发现当前需求缺失的维度。
    
    示例: "所有认证类需求都有密码重试锁定机制，当前需求没有" → 遗漏
    """
    # 1. 检索同类需求 (同 product_id + modules 任一匹配)
    similar_reqs = await knowledge_base.search_similar_requirements(
        query=srr["requirement"]["summary"],
        product_id=srr["requirement"].get("product_id", ""),
        modules=srr["requirement"].get("modules", []),
        top_k=10
    )
    if not similar_reqs:
        return {"gaps": [], "gap_score": 0.0, "passed": True}
    
    # 2. 提取同类需求的共同维度
    dimension_stats = {}
    for req in similar_reqs:
        for dim in extract_dimensions(req["srr"]):
            dimension_stats.setdefault(dim, {"count": 0, "examples": []})
            dimension_stats[dim]["count"] += 1
            if len(dimension_stats[dim]["examples"]) < 2:
                dimension_stats[dim]["examples"].append(req["srr"]["requirement"]["title"])
    
    # 3. 找出 60% 以上同类需求有但当前没有的维度
    total = len(similar_reqs)
    current_dims = set(extract_dimensions(srr))
    gaps = []
    for dim, stats in dimension_stats.items():
        if stats["count"] / total >= 0.6 and dim not in current_dims:
            gaps.append({
                "missing_dimension": dim,
                "evidence": f"历史 {total} 个同类需求中 {stats['count']} 个包含「{dim}」",
                "suggested_question": f"当前需求未提及「{dim}」。历史同类需求（如：{', '.join(stats['examples'])}）均包含此设计。是否需要添加？"
            })
    
    score = min(1.0, len(gaps) * 0.2)
    return {"gaps": gaps, "gap_score": score, "passed": score < 0.4}
```

### 4.4 KB 自动补全

#### 4.4.1 字段冲突优先级规则（⭐ 优化项5）

> **问题**：当 KB 自动补全的值与 SRR 中已有的值冲突时（如用户原文 `timeout=30s`，KB 命中相似需求 `timeout=60s`），原逻辑直接覆盖会导致用户原始意图丢失；反之完全忽略 KB 值又会丢失有价值的参考。

**字段冲突优先级**（从高到低）：

| 优先级 | 来源 | 标记 | 适用场景 |
|--------|------|------|---------|
| P0 | 用户当前需求原文（SRR 中已有非空值） | `USER_INPUT` | 任何场景，用户意图优先 |
| P1 | 人工回答 ClarifyAgent 问题后写入 | `HUMAN_ANSWERED` | KB 补全与原文冲突，经人工确认 |
| P2 | KB 自动补全（相似度 > 阈值） | `KB_INFERRED` | 仅当字段为空且无冲突时自动填充 |
| P3 | 需求模式库默认值 | `PATTERN_DEFAULT` | KB 未命中时的兜底建议 |
| P4 | LLM 推断（基于上下文） | `LLM_INFERRED` | 仅作为人工审核建议，不自动写入 |

**冲突处理决策矩阵**：

| 已有字段来源 | KB 补全建议 | 处理动作 |
|------------|------------|---------|
| 空（无值） | KB 命中 | 自动填充，标记 `KB_INFERRED`（原逻辑保留） |
| `USER_INPUT` | KB 命中且值不同 | **不覆盖**，将 KB 值作为 `suggested_value` 写入 `field_conflicts`，待人工回答 |
| `USER_INPUT` | KB 命中且值相同 | 合并来源标记（`USER_INPUT+KB_CORROBORATED`），提升 confidence |
| `HUMAN_ANSWERED` | KB 命中且值不同 | **不覆盖**，记录冲突到 `field_conflicts`，提示人工复核 |
| `KB_INFERRED` | 新一轮 KB 命中且值不同 | 取相似度更高的值；相似度相近时保留原值，避免抖动 |
| `PATTERN_DEFAULT` | KB 命中 | KB 优先，覆盖默认值并升级标记为 `KB_INFERRED` |

**实现扩展**（在 `kb_augment_requirement` 中应用）：

```python
FIELD_PRIORITY = {
    "USER_INPUT": 0,
    "HUMAN_ANSWERED": 1,
    "KB_INFERRED": 2,
    "PATTERN_DEFAULT": 3,
    "LLM_INFERRED": 4,
}

def _resolve_field_conflict(existing_value, existing_source, kb_value, kb_similarity):
    """字段冲突解决：返回 (final_value, final_source, conflict_record)"""
    if existing_value is None or existing_value == "":
        # 字段为空 → 直接填充
        return kb_value, "KB_INFERRED", None

    if existing_value == kb_value:
        # 值相同 → 合并来源，提升置信
        merged_source = f"{existing_source}+KB_CORROBORATED"
        return existing_value, merged_source, None

    # 值不同 → 按优先级处理
    existing_priority = FIELD_PRIORITY.get(existing_source, 0)
    if existing_priority <= FIELD_PRIORITY["HUMAN_ANSWERED"]:
        # 用户输入或人工回答 → 不覆盖，记录冲突待人工
        conflict = {
            "field": None,  # 由调用方填充
            "existing_value": existing_value,
            "existing_source": existing_source,
            "suggested_value": kb_value,
            "suggested_source": "KB_INFERRED",
            "kb_similarity": kb_similarity,
            "resolution": "pending_human"  # 待人工解决
        }
        return existing_value, existing_source, conflict
    elif existing_source == "KB_INFERRED":
        # 已有 KB 值 vs 新 KB 值 → 相似度更高的胜出，避免抖动
        # 注：原 augmentations 中已记录原 similarity，调用方需传入比较
        return kb_value, "KB_INFERRED", None  # 由调用方决定是否真的覆盖
    else:
        # PATTERN_DEFAULT / LLM_INFERRED → KB 优先
        return kb_value, "KB_INFERRED", None
```

**field_conflicts 在 SRR 中的位置**：

```json
{
  "requirement": { ... },
  "field_conflicts": [
    {
      "field": "data_constraints.timeout_seconds",
      "existing_value": 30,
      "existing_source": "USER_INPUT",
      "suggested_value": 60,
      "suggested_source": "KB_INFERRED",
      "kb_similarity": 0.88,
      "kb_source_req_id": "REQ-2023-015",
      "resolution": "pending_human"
    }
  ]
}
```

> `field_conflicts` 会在 §4.5 问题生成阶段被转换为针对每个冲突的人工问题，统一进入一次性问题列表。

#### 4.4.2 KB 自动补全实现

```python
async def kb_augment_requirement(srr, detected_issues, knowledge_base, threshold=0.85):
    """
    对检测到的缺口，先尝试 KB 检索补全。

    策略:
    - 缺失字段 → 检索同类需求的对应字段 → 相似度>阈值 → 自动填充
    - 遗漏维度 → 检索同类需求的该维度 → 标记为建议(不自动填充)
    - 字段冲突 → 按 §4.4.1 优先级规则处理，不覆盖 USER_INPUT/HUMAN_ANSWERED

    返回:
    - augmented_srr: 补全后的 SRR
    - augmentations: 补全记录 (含来源、相似度)
    - field_conflicts: 字段冲突列表（待人工解决）
    - unresolved_issues: KB 无法解决的 (需要人工)
    """
    augmented_srr = copy.deepcopy(srr)
    augmentations = []
    field_conflicts = augmented_srr.setdefault("field_conflicts", [])
    unresolved = []

    for missing in detected_issues["completeness"]["important_missing"]:
        similar = await knowledge_base.search_similar_requirements(
            query=srr["requirement"]["summary"],
            product_id=srr["requirement"].get("product_id", ""),
            modules=srr["requirement"].get("modules", []),
            top_k=5)
        filled = False
        for req in similar:
            source_value = get_nested_value(req["srr"], missing["field"])
            if source_value and req["similarity"] > threshold:
                # 检查字段冲突（§4.4.1）
                existing_value = get_nested_value(augmented_srr, missing["field"])
                existing_source = get_nested_source(augmented_srr, missing["field"]) or "USER_INPUT"
                final_value, final_source, conflict = _resolve_field_conflict(
                    existing_value, existing_source, source_value, req["similarity"]
                )
                set_nested_value(augmented_srr, missing["field"], final_value)
                set_nested_source(augmented_srr, missing["field"], final_source)
                augmentations.append({
                    "field": missing["field"],
                    "source_req_id": req["req_id"],
                    "similarity": req["similarity"],
                    "auto_filled": (conflict is None),
                    "source": final_source
                })
                if conflict:
                    conflict["field"] = missing["field"]
                    conflict["kb_source_req_id"] = req["req_id"]
                    field_conflicts.append(conflict)
                filled = True
                break
        if not filled:
            unresolved.append({"type": "missing_field", **missing})

    for gap in detected_issues["gaps"]["gaps"]:
        unresolved.append({"type": "missing_dimension", **gap})

    return {"augmented_srr": augmented_srr,
            "augmentations": augmentations,
            "field_conflicts": field_conflicts,
            "unresolved_issues": unresolved}
```

### 4.5 问题生成 — 分优先级无限制问题列表（v2.1 优化）⭐ 优化

> **设计决策（v2.1 变更）**：
> 原方案限制 `max_questions=5` 以减少"问卷疲劳"，但在实际复杂需求（多模块、多系统交互）中，
> 5 个问题远不足以覆盖所有 Critical/Important 问题，导致关键信息遗漏。
>
> **新策略**：Critical 和 Important 问题**不限条数**，全部展示给用户。
> Optional 类问题建议设较大上限（如 20 个）防止过度干扰。用户可一次批量回答或标记跳过。

**与旧方案的对比**：

| 维度 | 旧方案（v2.0） | 新方案（v2.1） |
|--------|----------------|----------------|
| Critical 问题 | 最多 5 个（和其他类型混排截断） | **不限条数**，全部展示 |
| Important 问题 | 最多 5 个（混合截断） | **不限条数**，全部展示 |
| Optional 问题 | 最多 5 个（混合截断） | 建议上限 20 个，允许用户标记"跳过" |
| 用户操作 | 最多回答 5 个问题 | 按优先级分组展示，批量回答 |
| 前端展示 | 简单列表 | 按优先级分组 + 分页（Critical 优先展示） |
| 信息遗漏风险 | ⚠️ 高——截断可能导致关键问题被丢弃 | ✅ 低——所有 Critical/Important 问题均展示 |

> **设计决策（基于 SpecFix 2025 论文 + ClarifyGPT 实践）**：
> 多轮对话在实际项目中落地难度大（测试人员不会耐心完成 3 轮问答）。
> 改为**一次性生成所有问题**，测试人员批量回答，系统一次性应用所有答案。
> 如果应用后质量仍不达标，再生成第二轮问题（最多 2 轮，非常罕见）。

```python
async def generate_clarification_questions_batch(
    srr: dict,
    knowledge_base,
    llm_client,
    max_questions: int | None = None,   # v2.1: None = 不限条数（Critical/Important 全部展示）
) -> dict:
    """
    一次性生成澄清问题列表（v2.1：Critical/Important 不限条数）。
    
    返回:
    {
        "status": "needs_clarification" | "passed",
        "questions": [  # Critical → Important → Optional 分组排序
            {
                "id": "Q001",
                "priority": "critical" | "important" | "optional",
                "category": "completeness" | "ambiguity" | "gap",
                "question": "需求未说明密码长度限制，请补充。",
                "field": "business_rules.password_policy",  # 对应 SRR 字段路径
                "context": "密码长度是生成边界值测试用例的必要信息。",
                "input_type": "text" | "select" | "multi_select",
                "options": ["8-16位", "6-20位", "自定义"],  # input_type=select 时
                "auto_filled": False,  # 是否已由 KB 自动补全
                "source": "completeness_check" | "ambiguity_detection" | "gap_detection"
            }
        ],
        "auto_augmentations": [  # KB 已自动补全的字段（展示给用户看）
            {"field": "preconditions.login_state", "source": "REQ-AUTH-003", "value": "..."}
        ],
        # ⭐ 统一接口：quality_report 嵌套结构，与 QualityGate（§5.2）输入对齐
        "quality_report": {
            "overall_score": 0.45,  # 当前质量评分（越高越好）
            "pass_threshold": 0.7
        },
        # ⭐ 统一接口：kb_status 嵌套结构，供 QualityGate._resolve_pass_score 判定冷启动
        "kb_status": {
            "kb_empty": false,                # 知识库整体为空
            "product_first_requirement": false # 当前产品首个需求
        },
        # ⭐ v2.1 新增：KB 访问日志
        "kb_access_log": [
            {
                "action": "search_similar_requirements",
                "query": "用户登录功能",
                "result_count": 3,
                "top_similarity": 0.92,
                "duration_ms": 45,
                "status": "success"
            }
        ]
    }
    
    策略:
    1. 三级检测（完整性 + 歧义性 + 遗漏）
    2. KB 自动补全能解决的 → 直接填充，不生成问题
    3. KB 无法解决的 → 生成问题，按优先级分组：
       - Critical 问题 → 不限条数，全部展示
       - Important 问题 → 不限条数，全部展示
       - Optional 问题 → 建议上限 20 个（默认），但允许用户标记"跳过"
    4. 如果 questions 为空 且 quality_score >= pass_threshold → status="passed"
    """
    # 1. 三级检测
    completeness = check_completeness(srr)
    ambiguity = await detect_ambiguity(srr, llm_client)
    gaps = await detect_gaps(srr, knowledge_base)
    
    # 2. KB 自动补全
    kb_aug = await kb_augment_requirement(srr, {
        "completeness": completeness,
        "ambiguity": ambiguity,
        "gaps": gaps
    }, knowledge_base)

    # 2.5 判断 KB 状态（冷启动模式判定，供 QualityGate §5.2 动态阈值使用）
    product_id = srr.get("requirement", {}).get("product_id", "PROD-DEFAULT")
    kb_empty = knowledge_base.is_empty()
    product_first_requirement = knowledge_base.is_product_first_requirement(product_id)
    cold_start = kb_empty or product_first_requirement
    cold_start_questions = _generate_cold_start_questions(srr) if cold_start else []

    # 3. 生成问题（只问 KB 无法解决的）
    questions = []
    qid = 1
    
    # Critical 完整性问题（最高优先级，必须问）
    for missing in completeness["critical_missing"]:
        if not _is_resolved(missing, kb_aug):
            questions.append({
                "id": f"Q{qid:03d}", "priority": "critical", "category": "completeness",
                "field": missing["field"],
                "question": f"需求缺少「{missing['description']}」，请补充。",
                "context": f"字段 {missing['field']} 是生成有效测试用例的必要信息。",
                "input_type": missing.get("input_type", "text"),
                "options": missing.get("options"),
                "auto_filled": False,
                "source": "completeness_check"
            })
            qid += 1
    
    # 歧义性问题
    for amb in ambiguity["ambiguities"]:
        if not _is_resolved(amb, kb_aug):
            questions.append({
                "id": f"Q{qid:03d}", "priority": "important", "category": "ambiguity",
                "field": amb["location"],
                "question": amb["suggested_question"],
                "context": amb.get("message", ""),
                "input_type": "text",
                "auto_filled": False,
                "source": "ambiguity_detection"
            })
            qid += 1
    
    # 遗漏问题（只问重要的）
    for issue in kb_aug["unresolved_issues"]:
        if issue["type"] == "missing_dimension":
            questions.append({
                "id": f"Q{qid:03d}", "priority": "optional", "category": "gap",
                "question": issue["suggested_question"],
                "context": issue.get("evidence", ""),
                "input_type": "select",
                "options": ["需要，具体规则如下：", "不需要", "不确定，请参考历史实现"],
                "auto_filled": False,
                "source": "gap_detection"
            })
            qid += 1
    
    # 冷启动问题合并（影响范围、边界条件、依赖确认）
    if kb_empty and cold_start_questions:
        for cq in cold_start_questions:
            if len(questions) >= max_questions:
                break
            questions.append({**cq, "id": f"Q{qid:03d}"})
            qid += 1
    
    # 4. 按优先级分组（Critical/Important 不限条数，Optional 设上限）
    priority_order = {"critical": 0, "important": 1, "optional": 2}
    questions.sort(key=lambda q: (priority_order.get(q["priority"], 3), q["id"]))
    
    # Critical + Important 问题：不限条数，全部保留
    # Optional 问题：设缺省上限 20 个（可配置），防止过度干扰
    optional_limit = 20
    critical_important = [q for q in questions if q["priority"] in ("critical", "important")]
    optional_questions = [q for q in questions if q["priority"] == "optional"][:optional_limit]
    questions = critical_important + optional_questions
    
    # 5. 计算质量评分
    quality_score = _calculate_quality(
        completeness, ambiguity, gaps, kb_aug, questions, kb_empty)
    
    status = "passed" if quality_score >= 0.7 or not questions else "needs_clarification"
    
    return {
        "status": status,
        "questions": questions,
        "auto_augmentations": kb_aug["augmentations"],
        # ⭐ 统一接口：quality_report 嵌套结构，与 QualityGate（§5.2）输入对齐
        "quality_report": {
            "overall_score": quality_score,
            "pass_threshold": 0.7,
        },
        # ⭐ 统一接口：kb_status 嵌套结构，与 QualityGate._resolve_pass_score（§5.2）输入对齐
        "kb_status": {
            "kb_empty": kb_empty,  # 知识库整体为空
            "product_first_requirement": product_first_requirement,  # 当前产品首个需求
        },
        "kb_augmentation_count": len(kb_aug["augmentations"]),
    }


def _is_resolved(issue: dict, kb_aug: dict) -> bool:
    """
    检查某个问题是否已被 KB 自动补全解决
    """
    # 对于 missing_field 类型（completeness 检测）
    field = issue.get("field")
    if field:
        for aug in kb_aug.get("augmentations", []):
            if aug.get("field") == field:
                return True
    # 对于 ambiguity / gap 类型，检查 auto_filled 标记
    if issue.get("auto_filled"):
        return True
    return False


def _generate_cold_start_questions(srr: dict) -> list[dict]:
    """
    冷启动模式：生成深度澄清问题（影响范围、边界条件、依赖确认）
    新产品第一个需求，没有历史知识库，需要更多人工确认
    """
    questions = []
    
    # 1. 涉及模块确认（多模块支持）
    modules = get_nested_value(srr, "modules", [])
    if not modules:
        questions.append({
            "priority": "critical",
            "category": "cold_start",
            "question": "这个需求涉及哪些模块？（可多选）例如：auth、payment、order 等",
            "context": "模块是知识库隔离和影响分析的基础，必须明确。",
            "input_type": "text",
            "auto_filled": False,
            "source": "cold_start_modules"
        })
            questions.append({
                "priority": "important",
                "category": "cold_start",
                "question": "「{}」这个功能的上线，会影响哪些已有功能？请列出所有受影响的功能模块。".format(
                    srr.get("requirement", {}).get("title", "该需求")
                ),
                "context": "新产品的第一个需求，没有历史用例可以参考，需要明确影响范围以确保测试覆盖。",
                "input_type": "text",
                "auto_filled": False,
                "source": "cold_start_impact"
            })
    
    # 2. 边界条件确认
    acceptance = get_nested_value(srr, "acceptance_criteria", [])
    if acceptance:
        questions.append({
            "priority": "important",
            "category": "cold_start",
            "question": "需求的边界条件是什么？比如：最大最小值、空值处理、并发上限、权限边界等。",
            "context": "边界条件往往是 bug 高发区，需要明确以确保测试用例覆盖。",
            "input_type": "text",
            "auto_filled": False,
            "source": "cold_start_boundary"
        })
    
    # 3. 依赖系统确认
    questions.append({
        "priority": "optional",
        "category": "cold_start",
        "question": "这个功能依赖哪些外部系统或服务？（如：数据库、第三方 API、消息队列、缓存等）",
        "context": "依赖系统的异常往往会影响本功能，需要确认以设计集成测试。",
        "input_type": "text",
        "auto_filled": False,
        "source": "cold_start_dependency"
    })
    
    # 4. 用户角色确认
    preconditions = get_nested_value(srr, "preconditions", [])
    questions.append({
        "priority": "important",
        "category": "cold_start",
        "question": "哪些用户角色会使用这个功能？不同角色的权限差异是什么？",
        "context": "不同角色的测试用例往往不同，需要明确角色划分。",
        "input_type": "text",
        "auto_filled": False,
        "source": "cold_start_roles"
    })
    
    # 5. 异常场景确认
    questions.append({
        "priority": "optional",
        "category": "cold_start",
        "question": "这个功能的异常场景有哪些？（如：网络超时、权限不足、数据异常等）",
        "context": "异常场景是测试用例的重要组成部分，需要提前明确。",
        "input_type": "text",
        "auto_filled": False,
        "source": "cold_start_exceptions"
    })
    
    return questions


def _calculate_quality(completeness, ambiguity, gaps, kb_aug, remaining_questions, kb_empty=False):
    """计算当前需求的质量评分（0.0-1.0）"""
    score = 1.0
    # 完整性扣分
    score -= len(completeness["critical_missing"]) * 0.15
    score -= len(completeness["important_missing"]) * 0.05
    # 歧义性扣分
    score -= len(ambiguity["ambiguities"]) * 0.1
    # 遗漏扣分
    score -= len(gaps["gaps"]) * 0.05
    # KB 补全加分
    score += len(kb_aug["augmentations"]) * 0.05
    
    # 冷启动模式加分：人工回答了深度澄清问题
    if kb_empty:
        answered_cold_start = sum(1 for q in remaining_questions if q.get("category") == "cold_start")
        score += answered_cold_start * 0.1  # 每回答一个冷启动问题加 0.1
    return max(0.0, min(1.0, score))


async def apply_answers_batch(
    srr: dict,
    questions: list[dict],
    answers: dict[str, str],  # {Q001: "答案是..."}
    auto_augmentations: list[dict]
) -> dict:
    """
    一次性应用所有答案，生成细化版 SRR。
    
    如果应用后质量仍不达标，可以选择：
    - 方案A：打回人工，要求补充更多信息
    - 方案B：生成第二轮问题（最多 2 轮）
    """
    refined = copy.deepcopy(srr)
    
    # 1. 先应用 KB 自动补全
    for aug in auto_augmentations:
        if aug.get("auto_filled"):
            set_nested_value(refined, aug["field"], aug.get("value"))
    
    # 2. 应用人工回答
    for q in questions:
        answer = answers.get(q["id"])
        if answer:
            # 根据 question 的 category 和 field 决定如何合并
            if q["category"] == "completeness":
                set_nested_value(refined, q["field"], answer)
            elif q["category"] == "ambiguity":
                # 歧义性：将答案合并到对应字段
                _merge_ambiguity_answer(refined, q, answer)
            elif q["category"] == "gap":
                if "需要" in answer:
                    # 用户确认需要该维度 → 在对应字段添加占位符
                    _add_gap_placeholder(refined, q)
    
    return refined
```

**与多轮对话模式的对比**：

| 维度 | 多轮对话（原方案） | 一次性问题列表（新方案） |
|--------|----------------|----------------|
| 用户交互 | 3-5 轮，每轮等待 LLM | 1 轮，批量回答 |
| 延迟 | 3-5 × LLM 调用 | 1 × LLM 调用 |
| 用户体验 | 对话疲劳，容易放弃 | 类似"填写问卷"，一次完成 |
| 实现复杂度 | 高（需维护对话状态） | 低（无状态） |
| 实际落地 | 难（测试人员不接受） | 易（符合现有工作流） |
| 参考来源 | — | SpecFix 2025 + ClarifyGPT |



---

### 4.6 交互示例（一次性问题列表）

```
══════════════════════════════════════════════════
  ClarifyAgent 澄清报告（一次性问题列表）
  需求: REQ-2024-003「用户注册功能」| 质量评分: 0.65 (❌ 未达标)
══════════════════════════════════════════════════

📋 质量评分: 0.65 (❌ 未达标)
  完整性: 0.65 | 歧义性: 0.70 | 遗漏度: 0.60

🤖 KB 自动补全 (2 项)
  ✅ preconditions ← REQ-2024-001 (相似度 0.92)
  ✅ non_functional.security ← REQ-2024-001 (相似度 0.89)

❓ 需要人工回答 (最多 5 个问题)

  [Q001] 🔴 Critical
  问题: 需求缺少「数据约束」，请补充字段定义（用户名长度、密码规则等）。
  上下文: 字段 requirement.data_constraints 是生成边界值测试用例的必要信息。
  输入类型: text

  [Q002] 🟡 Important
  问题: 「输入适当的验证码」中的「适当」具体指什么？
  上下文: 请提供验证码格式（位数、字符类型、有效期）。
  输入类型: text

  [Q003] 🟡 Important
  问题: 当前需求未提及「密码重试锁定机制」。
  证据: 历史 3 个同类需求（REQ-AUTH-001/002/005）均包含此设计。是否需要添加？
  输入类型: select
  选项: 1.需要，具体规则如下：  2.不需要  3.不确定，请参考历史实现

══════════════════════════════════════════════════
【使用方式】:
  1. 测试人员一次性回答所有问题（类似填写问卷）
  2. 系统应用所有答案，生成细化版 SRR
  3. 如果质量评分仍 < 0.7，生成第二轮问题（最多 2 轮）
══════════════════════════════════════════════════
### 4.7 完整端到端示例

以下展示从**原始需求文本**到**细化后 SRR v2**的完整过程。

#### 输入：原始需求文本

```text
需求ID: REQ-2024-003
标题: 用户注册功能优化
优先级: P1
涉及模块: auth, session

【功能描述】
优化用户注册流程，在原有邮箱注册基础上，增加手机号注册方式。
手机号注册需要发送验证码，验证码5分钟内有效，最多重试3次。

【验收标准】
- 用户可以通过手机号注册
- 验证码正确时注册成功
- 验证码错误时提示"验证码错误"
```

#### 步骤1: ReqAgent 解析 → SRR JSON (v1)

```json
{
  "srr_version": "1.0",
  "requirement": {
    "id": "REQ-2024-003",
    "title": "用户注册功能优化",
    "priority": "P1",
    "module": "auth",
    "summary": "在原有邮箱注册基础上，增加手机号注册方式，包含验证码校验"
  },
  "preconditions": ["用户未注册", "系统正常运行"],
  "flows": {
    "happy_path": {
      "steps": [
        {"order": 1, "action": "输入手机号", "expect": "显示验证码发送按钮"},
        {"order": 2, "action": "点击发送验证码", "expect": "收到短信验证码"},
        {"order": 3, "action": "输入验证码", "expect": "验证通过"},
        {"order": 4, "action": "点击注册", "expect": "注册成功，跳转首页"}
      ]
    },
    "alternate_flows": [
      {"name": "验证码错误", "trigger": "输入错误验证码", "expect": "提示「验证码错误」"},
      {"name": "验证码过期", "trigger": "超过5分钟输入", "expect": "提示「验证码已过期」"}
    ]
  },
  "boundary_conditions": [
    {"field": "手机号", "condition": "格式不正确", "expect": "前端拦截"},
    {"field": "验证码", "condition": "长度 ≠ 6位", "expect": "提示格式错误"}
  ],
  "business_rules": [
    {"id": "BR-01", "rule": "验证码5分钟内有效", "type": "timeout"},
    {"id": "BR-02", "rule": "最多重试3次", "type": "retry_limit"}
  ],
  "data_constraints": null,
  "non_functional": null,
  "relationships": null,
  "test_points_hint": [
    {"category": "functional", "point": "手机号注册正常流程"},
    {"category": "boundary", "point": "验证码长度边界"},
    {"category": "error", "point": "验证码错误处理"}
  ]
}
```

**ClarifyAgent 检测结果**：
- ❌ Critical 缺失: `data_constraints` 为空（影响边界值用例生成）
- ❌ Critical 缺失: `non_functional` 为空（缺少性能/安全需求）
- ⚠️ Important 缺失: `relationships` 为空（未说明是否依赖「发送短信」接口）
- ⚠️ 歧义性: 「最多重试3次」是指「验证码输错3次」还是「每天最多发送3次验证码」？

#### 步骤2: KB 自动补全

系统检索知识库，找到相似需求 `REQ-2023-015`（手机号绑定），自动补全：

```json
{
  "data_constraints": [
    {"field": "phone", "type": "string", "required": true, "constraints": "11位手机号", "example": "13800138000"},
    {"field": "verification_code", "type": "string", "required": true, "constraints": "6位数字", "example": "123456"}
  ],
  "non_functional": {
    "performance": "验证码发送P99 < 5s",
    "security": "验证码使用一次后失效, 防刷机制"
  },
  "relationships": [
    {"target": "REQ-SMS-001", "type": "depends_on", "description": "依赖短信发送接口"}
  ]
}
```

补全来源标记：`[KB_INFERRED from REQ-2023-015, similarity=0.91]`

#### 步骤3: 生成问题列表（KB 无法解决的）

```json
{
  "status": "needs_clarification",
  "questions": [
    {
      "id": "Q001",
      "priority": "critical",
      "category": "completeness",
      "question": "「最多重试3次」具体指什么？是验证码输错3次锁定，还是每天最多发送3次验证码？",
      "input_type": "select",
      "options": ["验证码输错3次锁定30分钟", "每天最多发送3次验证码", "同一验证码最多校验3次"],
      "context": "当前描述存在歧义，影响安全测试用例设计"
    },
    {
      "id": "Q002",
      "priority": "important",
      "category": "gap",
      "question": "历史同类需求（REQ-AUTH-001/002）均包含「密码强度校验」，本次注册是否需要？",
      "input_type": "select",
      "options": ["需要，规则如下：", "不需要", "手机号注册无需密码"],
      "context": "3/5 个同类需求包含此设计"
    }
  ],
  "auto_augmentations": [
    {"field": "data_constraints", "source": "REQ-2023-015", "similarity": 0.91},
    {"field": "non_functional", "source": "REQ-2023-015", "similarity": 0.91}
  ],
  "quality_report": {"overall_score": 0.68, "pass_threshold": 0.7},
  "kb_status": {"kb_empty": false, "product_first_requirement": false}
}
```

#### 步骤4: 人工回答（批量）

```json
{
  "Q001": "验证码输错3次锁定30分钟",
  "Q002": "不需要，手机号注册无需密码"
}
```

#### 步骤5: 应用答案 → SRR v2

```json
{
  "srr_version": "1.0",
  "requirement": { "...": "..." },
  "preconditions": ["用户未注册", "系统正常运行", "用户未触发安全锁定"],
  "flows": { "...": "..." },
  "boundary_conditions": [ "..." ],
  "business_rules": [
    {"id": "BR-01", "rule": "验证码5分钟内有效", "type": "timeout"},
    {"id": "BR-02", "rule": "验证码输错3次，锁定30分钟", "type": "security_lockout"}
  ],
  "data_constraints": [ "..." ],
  "non_functional": { "...": "..." },
  "relationships": [ "..." ],
  "_clarification": {
    "version": "v2",
    "refined_at": "2024-06-26T10:30:00Z",
    "questions_generated": 2,
    "questions_answered": 2,
    "kb_augmentations": 2,
    "quality_score_before": 0.68,
    "quality_score_after": 0.92
  }
}
```

**质量提升**：`0.68 → 0.92` ✅ 通过 QualityGate

#### 步骤6: QualityGate 检查 → 通过

```
✅ Schema 校验通过
✅ 关键字段非空检查通过
✅ 质量评分 0.92 >= 0.75 阈值
✅ 无未解决 Critical 问题
⚠️ 警告: happy_path 步骤数为 4（建议 >= 5，覆盖更多边界）

最终决定: ✅ 通过（仅警告，不阻断）
```

```

**与多轮对话的体验对比**：

| 维度 | 多轮对话（原方案） | 一次性问题列表（新方案） |
|------|----------------|----------------|
| 操作步骤 | 回答 Q1 → 等待 → 回答 Q2 → 等待... | 一次性回答所有问题 → 提交 |
| 等待次数 | 3-5 次 | 1 次 |
| 总耗时 | 3-5 × 10s = 30-50s | 1 × 10s = 10s |
| 用户流失率 | 高（对话疲劳） | 低（符合习惯） |
| 前端实现 | 对话 UI + 状态管理 | 问卷式表单（更简单） |

---

## 5. QualityGate 质量门禁

### 5.1 设计理念

借鉴 Auto-Validation 框架的 Constrain 步骤：**仅从包含明确验收标准的需求生成测试**。

> 关键发现：约束输入后测试数量下降 20-30%，但缺陷检测能力提升。

### 5.2 门禁检查项

```python
class RequirementQualityGate:
    """在 ClarifyAgent 之后、用例生成之前，执行最终质量检查。"""

    # 默认阈值与冷启动阈值（冷启动模式下 KB 为空，无法补全，阈值动态调低）
    DEFAULT_PASS_SCORE = 0.75
    COLD_START_PASS_SCORE = 0.60

    def _resolve_pass_score(self, srr: dict, clarify_result: dict) -> float:
        """根据上下文动态选择通过分数：
        - 冷启动模式（kb_empty=True 或当前 product_id 下无历史需求）→ 0.60
        - 正常模式 → 0.75
        避免 KB 为空时被门禁反复打回（最多 3 轮后强制输出的兜底仍可能产出低质量用例）。
        """
        kb_status = clarify_result.get("kb_status", {})
        if kb_status.get("kb_empty") or kb_status.get("product_first_requirement"):
            return self.COLD_START_PASS_SCORE
        return self.DEFAULT_PASS_SCORE

    def check(self, srr: dict, clarify_result: dict) -> dict:
        blockers = []
        warnings = []

        # 0. 动态选择通过阈值
        pass_score = self._resolve_pass_score(srr, clarify_result)
        cold_start = (pass_score == self.COLD_START_PASS_SCORE)

        # 1. Schema 校验
        if not self._validate_schema(srr):
            blockers.append("SRR 格式不符合 Schema 规范")

        # 2. 关键字段非空
        for field in ["requirement.title", "requirement.summary", "flows.happy_path"]:
            if not get_nested_value(srr, field):
                blockers.append(f"关键字段 {field} 为空")

        # 3. 质量评分达标（冷启动模式阈值放宽至 0.60）
        if clarify_result.get("quality_report"):
            score = clarify_result["quality_report"]["overall_score"]
            if score < pass_score:
                blockers.append(
                    f"质量评分 {score} 低于阈值 {pass_score}"
                    f"{'（冷启动模式）' if cold_start else ''}"
                )

        # 4. 无未解决的 Critical 问题
        if clarify_result.get("questions"):
            critical = [q for q in clarify_result["questions"] if q["priority"] == "critical"]
            if critical:
                blockers.append(f"仍有 {len(critical)} 个 Critical 问题未解决")

        # 5. 验收标准明确性 (警告)
        hints = srr.get("test_points_hint", [])
        if not hints or all(len(h.get("point", "")) <= 5 for h in hints):
            warnings.append("验收标准不够明确")

        # 6. happy_path 步骤数 (警告)
        steps = get_nested_value(srr, "flows.happy_path.steps") or []
        if len(steps) < 2:
            warnings.append("正常流程步骤过少")

        # 7. 冷启动模式提示（警告，非阻断）
        if cold_start:
            warnings.append(
                "冷启动模式：知识库为空或为该产品首个需求，"
                "质量门禁阈值已放宽至 0.60，建议人工重点审核生成结果"
            )

        passed = len(blockers) == 0
        return {"passed": passed, "blockers": blockers, "warnings": warnings,
                "pass_score": pass_score, "cold_start": cold_start,
                "recommendation": "✅ 通过" if passed else f"❌ 未通过: {'; '.join(blockers)}"}
```

### 5.3 门禁流程

```
ClarifyAgent 完成 → QualityGate 检查
  ├── Blocker 存在 → ❌ 打回 ClarifyAgent
  └── 仅 Warning → ⚠️ 放行（标记警告）
    └── 全部通过 → ✅ 进入生成流程
```

### 4.2.1 ClarifyAgent 工作流程序列图

```mermaid
flowchart TD
    A[SRR JSON 输入] --> B{完整性检测}
    B -->|缺失| C[标记 Critical 缺失]
    B -->|完整| D{歧义性检测}
    C --> D
    D -->|有歧义| E[标记 Ambiguity]
    D -->|无歧义| F{遗漏检测}
    E --> F
    F -->|有遗漏| G[标记 Gap]
    F -->|无遗漏| H[KB 自动补全]
    G --> H
    H --> I{KB 命中?}
    I -->|是| J[自动填充 + 标记 KB_INFERRED]
    I -->|否| K{是否需要人工?}
    J --> K
    K -->|是| L[生成问题列表 最多5个]
    K -->|否| M[质量评分]
    L --> N[人工回答批量问题]
    N --> O[应用答案 + 需求细化]
    O --> M
    J --> M
    M --> P{质量达标?}
    P -->|是| Q[输出 SRR v2 ✅]
    P -->|否| R{迭代次数 < 3?}
    R -->|是| B
    R -->|否| S[强制输出 + 警告 ⚠️]
    S --> Q
```

**流程图说明**：
- 绿色路径：KB 自动补全成功，无需人工 → 最快 10s 完成
- 黄色路径：需要人工回答问题 → 取决于人工响应速度
- 红色路径：3 轮迭代仍不达标 → 强制输出，标记质量风险
```


### 5.4 错误处理

QualityGate 在以下情况下会返回 `blockers`（阻断通过）：

| 错误类型 | 触发条件 | 处理建议 |
|---------|---------|---------|
| `SCHEMA_INVALID` | SRR JSON 不符合 Schema 规范 | 返回 ReqAgent 重新解析 |
| `CRITICAL_FIELD_MISSING` | 有关键字段（Critical 级）缺失 | 进入 ClarifyAgent 澄清流程 |
| `QUALITY_SCORE_LOW` | 质量评分 < 阈值（正常 0.75 / 冷启动 0.60） | 返回 ClarifyAgent 重新澄清 |
| `UNRESOLVED_CRITICAL_QUESTIONS` | 仍有未解决的 Critical 问题 | 提示用户回答所有 Critical 问题 |
| `LLM_CALL_FAILED` | LLM API 调用失败 | 重试 3 次后降级到规则模式 |

**降级策略**：
```
LLM 可用 → 完整质量检查（Schema + 完整性 + 歧义性 + 遗漏）
    ↓ LLM 不可用
规则模式 → 仅执行 Schema 校验 + 完整性检查（基于 Checklist）
    ↓ 规则模式仍失败
强制通过 → 标记 `QUALITY_GATE_SKIPPED=true`，记录警告日志
```

**错误响应格式**（当 QualityGate 阻断时返回给调用方）：

```json
{
  "status": "blocked",
  "stage": "quality_gate",
  "blockers": [
    "质量评分 0.62 低于阈值 0.75",
    "仍有 1 个 Critical 问题未解决: Q001"
  ],
  "suggestions": [
    "请回答所有 Critical 问题后重新提交",
    "建议补充 data_constraints 字段以提升用例生成质量"
  ],
  "clarify_result": {
    "questions": [ "..." ],
    "quality_score": 0.62
  }
}
```

---

## 6. 需求模式库

需求模式库存储**同类需求的抽象维度集合**，用于 ClarifyAgent 的遗漏检测。

### 6.0 与测试模式库的职责边界（⭐ 优化项10）

> **问题**：系统中存在两个"模式库"，容易混淆：
> - **需求模式库**（本节，doc-01 §6）
> - **测试模式库**（doc-03 §7.1 Level 2）
>
> 两者职责不同、使用阶段不同、存储内容不同，需明确边界避免误用。

| 维度 | 需求模式库（本节） | 测试模式库（doc-03 §7.1） |
|------|-------------------|-------------------------|
| **存储内容** | 同类需求的**抽象维度集合**（如 auth 类需求应包含"密码重试锁定"、"会话管理"等维度） | 已知**测试用例骨架模式**（如"状态机测试模式"→ 状态转移测试模板） |
| **回答的问题** | "这类需求**应该考虑哪些方面**？"（需求完整性） | "这类功能**该怎么测**？"（测试策略） |
| **使用阶段** | 需求解析阶段（ClarifyAgent 遗漏检测，§4.3.3） | 用例生成阶段（GenAgent Prompt 中的"测试模式参考"） |
| **使用方** | ClarifyAgent | GenAgent |
| **数据结构** | `{required_dimensions, common_boundary, risk_points}` | `{skeleton_steps, assertion_templates, data_generators}` |
| **更新来源** | 历史需求入库时提取维度 + 人工维护 | 历史用例聚类 + error_case 反向反馈（doc-03 §7.2.2） |
| **典型示例** | `auth` 模式 → "密码加密存储"是必备维度 | `auth_test` 模式 → "登录失败 5 次后锁定"的测试步骤骨架 |
| **KB 表名** | `requirement_patterns` | `test_patterns` |

**协作关系**（非竞争关系）：

```
需求解析阶段                          用例生成阶段
─────────────                       ─────────────
ClarifyAgent                        GenAgent
  │                                   │
  ├─ 查询需求模式库                    ├─ 查询测试模式库
  │  → 发现 auth 类需求               │  → 匹配 auth_test 模式
  │     缺少"会话管理"维度            │  → 获取测试骨架（步骤模板）
  │  → 提示用户补充                   │  → 填充当前需求的具体数据
  │                                   │
  └─ SRR 完整后传递给 GenAgent ──────┘
```

**避免误用的规则**：
1. 需求模式库**不包含**测试步骤或断言模板（那是测试模式库的职责）
2. 测试模式库**不包含**需求维度清单（那是需求模式库的职责）
3. 两者通过 `module` / `pattern_id` 关联，但物理上独立存储
4. error_case 反馈**只更新测试模式库**（doc-03 §7.2.2），不直接更新需求模式库；需求模式库的更新来自需求入库时的维度提取

### 6.1 预定义模式

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
    "crud": {
        "name": "CRUD 操作类",
        "modules": ["user", "order", "product", "article"],
        "required_dimensions": [
            "创建/查询/更新/删除流程", "数据校验规则", "权限控制", "分页/排序"
        ],
        "common_boundary": ["字段长度边界", "空值/null处理", "并发更新冲突"],
        "risk_points": ["越权操作", "数据丢失", "并发冲突"]
    },
    "payment": {
        "name": "支付交易类",
        "modules": ["payment", "transaction", "refund"],
        "required_dimensions": [
            "支付/退款流程", "支付状态机", "金额计算规则",
            "交易安全（签名/加密）", "幂等性保证", "对账机制"
        ],
        "common_boundary": ["金额边界（0/最小/最大）", "精度边界", "并发支付", "超时处理"],
        "risk_points": ["重复支付", "金额篡改", "退款超时", "对账不一致"]
    }
}
```

### 6.2 模式匹配

```python
class RequirementPatternMatcher:
    def match_pattern(self, srr: dict) -> Optional[dict]:
        """根据 module 和语义匹配需求模式"""
        module = srr.get("requirement", {}).get("module", "").lower()
        for pattern_id, pattern in REQUIREMENT_PATTERNS.items():
            if module in pattern["modules"]:
                return {"pattern_id": pattern_id, **pattern}
        # 语义匹配兜底
        # ...
        return None
```

### 6.3 持续积累

需求模式库随着系统使用持续积累：
- 每次需求入库时，自动提取维度并更新模式统计
- 模式使用频率和成功率作为模式质量的反馈
- 低频/低成功率模式自动降权

---

## 优化需求上下文自动补全（⭐ 核心增强）

> **问题**：当收到优化需求（如"优化登录流程"）时，需求文档通常只描述"改了什么"，不包含优化前的完整状态，导致 AI 无法理解完整上下文，影响范围不明确。
>
> **方案**：在 ReqAgent 解析阶段，自动检测优化需求，从知识库检索原始需求 + 历史用例，补全上下文。

### 检测逻辑：是否为优化需求

```python
OPTIMIZATION_KEYWORDS = [
    "优化", "改进", "升级", "提升", "增强", "完善",
    "修改", "调整", "重构", "改造", "迭代", "演进",
    "optimize", "improve", "enhance", "upgrade", "refactor", "modify"
]

def detect_optimization_intent(srr: dict, raw_text: str) -> dict:
    """
    检测当前需求是否为优化/变更需求
    返回：{"is_optimization": bool, "confidence": float, "keywords_matched": list}
    """
    title = srr.get("requirement", {}).get("title", "")
    summary = srr.get("requirement", {}).get("summary", "")
    combined_text = f"{title} {summary} {raw_text}"
    
    matched = [kw for kw in OPTIMIZATION_KEYWORDS if kw in combined_text]
    
    # 置信度计算
    if len(matched) >= 2:
        confidence = 0.95
    elif len(matched) == 1:
        # 检查是否是标题中明确提到（如"优化登录流程"）
        if any(kw in title for kw in matched):
            confidence = 0.90
        else:
            confidence = 0.70
    else:
        confidence = 0.0
    
    # LLM 辅助确认（当置信度在模糊区间时）
    if 0.5 < confidence < 0.9:
        prompt = f"""
        判断以下需求是否为对已有功能的优化/修改/升级需求（而非全新需求）：

        需求标题：{title}
        需求概述：{summary}

        只回答 yes 或 no，并给出理由。
        """
        llm_response = call_llm(prompt, temperature=0.0)
        if "yes" in llm_response.lower():
            confidence = 0.90
        else:
            confidence = 0.10
    
    return {
        "is_optimization": confidence > 0.7,
        "confidence": confidence,
        "keywords_matched": matched
    }
```

### 上下文补全流程

```
优化需求 SRR
    │
    ▼
① 检测优化意图 (detect_optimization_intent)
    │
    ├─ 不是优化需求 → 跳过，进入正常流程
    │
    └─ 是优化需求 ↓
② 识别被优化的原始需求
    │  策略1: 精确匹配（需求标题/ID中明确提到被优化的需求）
    │  策略2: 语义检索（在知识库中找最相似的历史需求）
    │  策略3: 模块+关键词联合检索
    │
    ▼
③ 检索原始需求完整上下文
    │  - 原始需求的 SRR JSON（优化前的完整功能描述）
    │  - 原始需求的历史测试用例（了解已有测试覆盖）
    │  - 原始需求的版本链（了解历史变更记录）
    │
    ▼
④ 生成需求差异化分析 (Requirement Diff)
    │  - 新增的功能点
    │  - 删除的功能点
    │  - 修改的功能点
    │  - 受影响的关联模块
    │
    ▼
⑤ 影响范围分析 (Impact Analysis)
    │  - 需要更新的测试用例（直接受影响）
    │  - 需要新增的测试用例（新功能点）
    │  - 需要删除的测试用例（删除的功能点）
    │  - 需要回归的测试用例（间接受影响）
    │
    ▼
⑥ 补全 SRR JSON（新增 optimization_context 字段）
    │
    ▼
⑦ 将影响分析结果传递给 GenAgent（指导用例生成范围）
```

### 识别被优化的原始需求

```python
def find_original_requirement(optimization_srr: dict, pg_conn, embedding_model) -> dict:
    """
    给定优化需求的 SRR，找到它优化的原始需求
    返回：原始需求的完整信息（SRR JSON + 历史用例 + 版本链）
    """
    title = optimization_srr.get("requirement", {}).get("title", "")
    modules = optimization_srr.get("requirement", {}).get("modules", []) or []
    product_id = optimization_srr.get("requirement", {}).get("product_id", "")
    summary = optimization_srr.get("requirement", {}).get("summary", "")
    
    # 策略1：精确匹配（标题中明确提到，如"优化登录功能" → 找"登录功能"）
    import re
    # 提取被优化的对象（去掉优化关键词）
    optimized_object = title
    for kw in OPTIMIZATION_KEYWORDS:
        optimized_object = optimized_object.replace(kw, "").strip()
    # 去掉标点符号，合并空格
    optimized_object = re.sub(r'[：:,，。"\'\]\ [\)\(【】\s]+', ' ', optimized_object)
    optimized_object = re.sub(r'\s+', ' ', optimized_object).strip()
    
    cur = pg_conn.cursor()
    exact_matches = []
    if optimized_object and modules:
        # 多模块查询：使用 ANY() 匹配 modules 数组中任意一个模块
        cur.execute("""
            SELECT id, title, srr_json, version, status
            FROM requirements
            WHERE product_id = %s
              AND module = ANY(%s)
              AND status = 'archived'
              AND title ILIKE %s
            ORDER BY version DESC
            LIMIT 5
        """, (product_id, modules, f"%{optimized_object}%"))
    exact_matches = cur.fetchall()
    
    if exact_matches:
        # 找到最匹配的（通常是最新版本）
        original = exact_matches[0]
        return {
            "match_strategy": "exact_title",
            "original_req": original,
            "confidence": 0.95
        }
    
    # 策略2：语义检索（在知识库中找最相似的历史需求）
    query_embedding = embedding_model.encode(summary)
    similar_reqs = vector_search(
        query_embedding=query_embedding,
        collection='requirements',
        top_k=5,
        filters={"product_id": product_id, "module": {"$in": modules}, "status": "archived"},
        pg_conn=pg_conn
    )
    
    if similar_reqs and similar_reqs[0]["similarity"] > 0.80:
        original_id = similar_reqs[0]["req_id"]
        cur.execute("SELECT * FROM requirements WHERE id = %s", (original_id,))
        return {
            "match_strategy": "semantic_similarity",
            "original_req": cur.fetchone(),
            "confidence": similar_reqs[0]["similarity"],
            "similar_reqs": similar_reqs
        }
    
    # 策略3：都没找到 → 标记为"新功能"（可能用户描述不准确）
    return {
        "match_strategy": "none",
        "original_req": None,
        "confidence": 0.0,
        "message": "未找到被优化的原始需求，建议人工确认"
    }
```

### 补全 SRR JSON（新增 optimization_context 字段）

```python
def augment_srr_with_optimization_context(
    srr: dict,
    original_req: dict,
    original_cases: list,
    diff: dict,
    impact: dict
) -> dict:
    """
    在 SRR JSON 中新增 optimization_context 字段，
    为 GenAgent 提供完整的优化上下文
    """
    srr["optimization_context"] = {
        # 原始需求信息
        "original_requirement": {
            "id": original_req["id"],
            "title": original_req["title"],
            "version": original_req["version"],
            "srr_json": original_req["srr_json"],  # 完整原始 SRR
        },
        # 历史测试用例（已有覆盖）
        "original_test_cases": [
            {
                "id": c["id"],
                "title": c["title"],
                "type": c["type"],
                "status": c["status"]
            }
            for c in original_cases
        ],
        # 需求差异化分析
        "diff": diff,  # 来自 generate_requirement_diff()
        # 影响分析结果
        "impact_analysis": impact,  # 来自 analyze_change_impact()
        # 给 GenAgent 的提示
        "generation_guidance": {
            "must_update_cases": [c["id"] for c in impact["direct"]["test_cases"]],
            "must_add_cases": diff["added"],  # 新增功能点需要新用例
            "can_delete_cases": diff["removed"],  # 删除功能点可以删除用例
            "regression_scope": impact["regression_scope"],  # 回归测试范围
        }
    }
    
    # 同时更新 change_type
    srr["requirement"]["change_type"] = "optimized"
    srr["requirement"]["supersedes"] = original_req["id"]
    
    return srr
```

### 与 ClarifyAgent 的集成

优化需求的澄清流程需要**特殊对待**：

```python
async def clarify_optimization_requirement(
    srr_with_context: dict,
    original_req: dict,
    kb
) -> dict:
    """
    优化需求的澄清流程（与全新需求不同）
    """
    questions = []
    
    # 1. 确认原始需求识别是否正确
    questions.append({
        "id": "OPT-001",
        "priority": "critical",
        "category": "context_confirmation",
        "question": f"系统识别到本需求是对「{original_req['title']}」(ID: {original_req['id']}) 的优化。是否正确？如果不正确，请指出被优化的原始需求。",
        "context": f"原始需求摘要：{original_req['srr_json'].get('summary', '')[:200]}..."
    })
    
    # 2. 确认影响范围
    impact = srr_with_context["optimization_context"]["impact_analysis"]
    if impact["direct"]["test_cases"]:
        questions.append({
            "id": "OPT-002",
            "priority": "important",
            "category": "impact_confirmation",
            "question": f"本次优化会影响以下 {len(impact['direct']['test_cases'])} 个已有测试用例，是否需要同步更新？\n" +
                       "\n".join([f"- {c['id']}: {c['title']}" for c in impact["direct"]["test_cases"][:5]]),
            "context": "如果这些用例不更新，执行时会失败"
        })
    
    # 3. 确认回归测试范围
    regression = srr_with_context["optimization_context"]["impact_analysis"]["regression_scope"]
    if regression:
        questions.append({
            "id": "OPT-003",
            "priority": "important",
            "category": "regression_scope",
            "question": f"本次优化可能影响 {len(regression)} 个回归测试用例，是否需要在本次测试中一并执行？",
            "context": "回归测试确保优化没有破坏已有功能"
        })
    
    # 4. 正常的完整性/歧义性检测（但利用原始需求上下文）
    #    （复用原有的 check_completeness / detect_ambiguity / detect_gaps）
    
    return questions
```

---

## 7. 业界借鉴

| 实践来源 | 借鉴点 |
|----------|--------|
| **ClarifyGPT** (ACL 2024) | 模糊需求检测 + 针对性澄清问题生成；仅对模糊需求提问 |
| **Auto-Validation** (2025) | Constrain 步骤：约束输入质量；Traceability/Consistency/Completeness 三指标 |
| **Meta TestGen-LLM** | 从需求提取 test_points_hint |
| **Gherkinizer** | NL → 结构化转换验证 |
| **ACL 2024** | NL 需求直接生成测试用例有效性验证 (F1 0.78+) |

---

## 8. 实施建议

1. **先跑通 Markdown → SRR JSON 主链路**，再扩展 JIRA/飞书等输入源
2. **ClarifyAgent 优先实施**：三级检测 + KB 补全是整个系统质量的基础
3. **需求模式库从 3-5 个高频模式起步**（auth/crud/payment），随使用持续积累
4. **QualityGate 要严格**：宁可不生成也不要用低质量需求生成垃圾用例
5. **需求版本管理**：每次变更生成新 SRR，旧版本标记 `superseded`

## 9. 附录：辅助函数实现

为确保文档中的代码示例可直接运行，以下是关键辅助函数的完整实现。

### 9.1 嵌套字段访问工具

```python
import re
from typing import Any, Optional

def get_nested_value(data: dict, field_path: str) -> Any:
    """
    获取嵌套字典中的值，支持 a.b.c 和 a[0].b 两种路径。
    
    Args:
        data: 嵌套字典
        field_path: 字段路径，如 "flows.happy_path.steps" 或 "business_rules[0].rule"
    
    Returns:
        字段值，不存在时返回 None
    
    Examples:
        >>> get_nested_value({"a": {"b": 1}}, "a.b")
        1
        >>> get_nested_value({"a": [{"b": 1}]}, "a[0].b")
        1
    """
    if not field_path or not data:
        return None
    
    current = data
    # 解析路径: a.b[0].c
    pattern = r'([^.\[\]]+)|\[(\d+)\]'
    tokens = re.findall(pattern, field_path)
    
    for token in tokens:
        key = token[0] or int(token[1])
        if isinstance(key, str):
            if not isinstance(current, dict) or key not in current:
                return None
            current = current[key]
        else:  # 整数索引
            idx = key
            if not isinstance(current, list) or idx >= len(current):
                return None
            current = current[idx]
    
    return current


def set_nested_value(data: dict, field_path: str, value: Any) -> dict:
    """
    设置嵌套字典中的值，如果路径不存在则自动创建。
    
    Args:
        data: 嵌套字典（会原地修改）
        field_path: 字段路径
        value: 要设置的值
    
    Returns:
        修改后的 data（同级引用，方便链式调用）
    
    Examples:
        >>> d = {}
        >>> set_nested_value(d, "a.b.c", 1)
        {'a': {'b': {'c': 1}}}
    """
    if not field_path:
        return data
    
    current = data
    pattern = r'([^.\[\]]+)|\[(\d+)\]'
    tokens = re.findall(pattern, field_path)
    
    for i, token in enumerate(tokens):
        key = token[0] or int(token[1])
        is_last = (i == len(tokens) - 1)
        
        if is_last:
            if isinstance(key, str):
                current[key] = value
            else:
                # 列表索引
                while len(current) <= key:
                    current.append(None)
                current[key] = value
        else:
            next_key = tokens[i+1][0] or int(tokens[i+1][1])
            if isinstance(key, str):
                if key not in current or not isinstance(current[key], (dict, list)):
                    # 根据下一个 key 的类型决定创建 dict 还是 list
                    current[key] = {} if isinstance(next_key, str) else []
                current = current[key]
            else:
                idx = key
                while len(current) <= idx:
                    current.append({} if isinstance(next_key, str) else [])
                current = current[idx]
    
    return data


def find_contexts(srr: dict, keyword: str) -> list[dict]:
    """
    在 SRR JSON 中查找包含关键词的上下文。
    
    Args:
        srr: SRR JSON 字典
        keyword: 要查找的关键词
    
    Returns:
        包含关键词的位置列表，每个元素包含 location 和 text
    """
    results = []
    text_fields = ['requirement.title', 'requirement.summary', 'flows.happy_path.steps']
    
    for field_path in text_fields:
        value = get_nested_value(srr, field_path)
        if not value:
            continue
        
        # 处理不同类型的值
        texts = []
        if isinstance(value, str):
            texts = [value]
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    texts.append(item)
                elif isinstance(item, dict):
                    # 尝试提取 action 和 expect 字段
                    for k in ['action', 'expect', 'name', 'trigger']:
                        if k in item:
                            texts.append(str(item[k]))
        
        for text in texts:
            if keyword in text:
                results.append({
                    'location': field_path,
                    'text': text,
                    'keyword': keyword
                })
    
    return results
```

### 9.2 LLM 歧义检测辅助函数

```python
import json
from typing import List, Dict

async def llm_detect_ambiguities(srr: dict, llm_client) -> List[Dict]:
    """
    使用 LLM 检测 SRR 中的歧义性问题。
    
    Args:
        srr: SRR JSON 字典
        llm_client: LLM 客户端（支持 .complete() 方法）
    
    Returns:
        歧义性列表，每个元素包含 type, location, message
    """
    prompt = f"""
你是软件需求分析专家。请仔细分析以下结构化需求（SRR JSON），找出所有歧义性问题。

SRR JSON:
{json.dumps(srr, ensure_ascii=False, indent=2)}

请找出以下类型的歧义性：
1. 矛盾描述（同一字段在不同位置描述不一致）
2. 逻辑断裂（流程步骤之间缺少必要的连接或判断）
3. 缺少预期结果（步骤描述了操作，但没有说明期望结果）
4. 未定义的术语（使用了业务术语但没有解释）
5. 条件不完整（if-then 逻辑缺少 else 分支）

输出格式（JSON数组）:
[
  {{
    "type": "contradiction|logic_gap|missing_expectation|undefined_term|incomplete_condition",
    "location": "字段路径",
    "original_text": "原文片段",
    "message": "歧义性描述",
    "suggested_question": "建议向用户提出的问题"
  }}
]

如果未发现歧义性，返回空数组 []。
"""
    
    try:
        response = await llm_client.complete(
            prompt=prompt,
            temperature=0.1,
            max_tokens=1000,
            response_format={"type": "json_object"}
        )
        result = json.loads(response.text)
        return result if isinstance(result, list) else []
    except Exception as e:
        print(f"LLM 歧义检测失败: {e}")
        return []
```

### 9.3 配置示例

#### 环境变量配置 (.env)

```bash
# ReqAgent 配置
REQ_AGENT_MODEL=deepseek-v4
REQ_AGENT_TEMPERATURE=0.1
REQ_AGENT_MAX_TOKENS=2000

# ClarifyAgent 配置
# CLARIFY_MAX_QUESTIONS 已废弃 v2.1 → Critical/Important 不限条数，Optional 上限见 CLARIFY_OPTIONAL_LIMIT
CLARIFY_OPTIONAL_LIMIT=20
CLARIFY_MAX_ITERATIONS=3
CLARIFY_KB_SIMILARITY_THRESHOLD=0.85
CLARIFY_ENABLE_LLM_AMBIGUITY=true

# QualityGate 配置
QUALITY_GATE_PASS_SCORE=0.75
QUALITY_GATE_STRICT_MODE=false

# 数据库连接
DATABASE_URL=postgresql://user:pass@localhost:5432/ai_test_gen
VECTOR_DB_TYPE=milvus
MILVUS_HOST=localhost
CHROMADB_PORT=8000

# 日志
LOG_LEVEL=INFO
LOG_FILE=./logs/req_agent.log
```

#### Python 配置（config.py）

```python
from pydantic import BaseModel, Field
from typing import Optional

class ReqAgentConfig(BaseModel):
    """ReqAgent 配置"""
    model: str = "deepseek-v4"
    temperature: float = 0.1
    max_tokens: int = 2000
    timeout: int = 30

class ClarifyAgentConfig(BaseModel):
    """ClarifyAgent 配置"""
    max_questions: int = 5
    max_iterations: int = 3
    kb_similarity_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    enable_llm_ambiguity: bool = True
    
    # 质量评分权重
    critical_weight: float = 3.0
    important_weight: float = 1.0
    optional_weight: float = 0.3

class QualityGateConfig(BaseModel):
    """QualityGate 配置"""
    pass_score: float = Field(default=0.75, ge=0.0, le=1.0)
    strict_mode: bool = False
    max_warnings: int = 5  # 超过此数量警告则阻断

class AppConfig(BaseModel):
    """应用总配置"""
    req_agent: ReqAgentConfig = Field(default_factory=ReqAgentConfig)
    clarify_agent: ClarifyAgentConfig = Field(default_factory=ClarifyAgentConfig)
    quality_gate: QualityGateConfig = Field(default_factory=QualityGateConfig)
    
    # 数据库
    database_url: str = "postgresql://localhost/ai_test_gen"
    vector_db_type: str = "milvus"  # 统一使用 Milvus（ChromaDB 已弃用）
    
    # 日志
    log_level: str = "INFO"
    log_file: Optional[str] = None

# 从环境变量加载
import os

def load_config() -> AppConfig:
    return AppConfig(
        req_agent=ReqAgentConfig(
            model=os.getenv("REQ_AGENT_MODEL", "deepseek-v4"),
            temperature=float(os.getenv("REQ_AGENT_TEMPERATURE", "0.1")),
            max_tokens=int(os.getenv("REQ_AGENT_MAX_TOKENS", "2000")),
        ),
        clarify_agent=ClarifyAgentConfig(
            # v2.1: Critical/Important 不限条数，仅 Optional 设上限
            optional_limit=int(os.getenv("CLARIFY_OPTIONAL_LIMIT", "20")),
            max_iterations=int(os.getenv("CLARIFY_MAX_ITERATIONS", "3")),
            kb_similarity_threshold=float(os.getenv("CLARIFY_KB_SIMILARITY_THRESHOLD", "0.85")),
            enable_llm_ambiguity=os.getenv("CLARIFY_ENABLE_LLM_AMBIGUITY", "true").lower() == "true",
        ),
        quality_gate=QualityGateConfig(
            pass_score=float(os.getenv("QUALITY_GATE_PASS_SCORE", "0.75")),
            strict_mode=os.getenv("QUALITY_GATE_STRICT_MODE", "false").lower() == "true",
        ),
        database_url=os.getenv("DATABASE_URL", "postgresql://localhost/ai_test_gen"),
        vector_db_type=os.getenv("VECTOR_DB_TYPE", "milvus"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        log_file=os.getenv("LOG_FILE"),
    )
```

---

## 10. v2.18 实现更新（需求分析流水线）

> 本节记录 v2.18 版本中需求分析流水线的实际实现变更，作为对前文设计文档的补充与对齐。

### 10.1 文档解析增强（docx 表格 + 图片 + 多模态）

**问题背景**：原 `_extract_docx_text` 仅提取段落文本，丢失表格和图片，导致大文档（如 30 万字 PRD）解析后内容不完整。

**实现**（`backend/app/api/requirements.py`）：

| 变更 | 说明 |
|------|------|
| `_table_to_markdown()` | 新增：将 docx 表格转为 Markdown 表格语法 |
| `_extract_docx_text()` | 重写：按 `body.iterchildren()` 遍历 `<w:p>`/`<w:tbl>` 保持原始顺序；返回 `tuple[str, list[dict]]`（文本 + 原始图片） |
| `_describe_docx_image()` | 新增：提取 `<a:blip r:embed>` → base64 → 调用多模态 LLM 生成描述；同时保存原始图片数据 |
| `raw_images` 字段 | `Requirement` 模型新增 `raw_images: JSON` 字段，存储 base64 data_url 供前端展示对比 |

**多模态集成**：
- `llm_client.call_vision()`：通过 `litellm.acompletion` 调用视觉 LLM（OpenAI Vision API 格式）
- `KBSetting` 表存储 `vision_llm_provider` 配置
- `GET/PUT /api/knowledge/vision-llm` 端点管理多模态模型配置
- 图片识别提示词纳入 `prompt_manager` 管理（`vision` 模块）

### 10.2 ClarifyAgent 关键 Bug 修复

| Bug | 根因 | 修复 |
|-----|------|------|
| KB 查询完全失效 | `clarify_agent.py` 缺少 `import time`，`NameError` 导致整个 `clarify()` 崩溃 | 添加 `import time` |
| 日志格式错误 | loguru 使用 `{}` 占位符，代码中用了 `%d`/`%s` | 6 处全部替换为 `{}` |
| ClarifyAgent 失败覆盖首次解析 | 失败后 `needs_clarify=False` 触发 regenerate，二次解析的 SRR 与已保存 FP 不一致 | 改为仅有 KB 补全信息时才重新解析 |
| `kb_empty` 误报 | 失败时默认 `True`，误导用户认为知识库为空 | 改为 `None`（未知） |
| `existing_source` 默认值 | 阻止 KB 覆盖 LLM 推断字段 | 未标记的值视为 `LLM_INFERRED` |
| `duration_ms` 不准 | 使用总耗时而非单条查询耗时 | 添加 per-query 计时 |
| LLM 查询生成失败致 KB 全失效 | `_generate_kb_queries` 异常导致无查询 | 实现模板降级 fallback |

### 10.3 错误可见性原则（流水线中断机制）

**设计原则**：流水线中任何关键步骤失败必须中断流水线、标记任务 `failed`、携带 `error` 字段，禁止静默吞错后标记 `completed`。

**实现**（`_run_requirement_analysis`）：

| 关键步骤 | 失败处理 |
|----------|----------|
| ClarifyAgent 失败 | 保存基础 SRR+FP → `task.status=failed` → `return` 中断 |
| FP 保存失败 | `task.status=failed` → `return` 中断 |
| 最终 SRR 生成失败 | 降级使用首次解析结果 + `result.degraded=true` + `degraded_reason` 告知前端 |
| regenerate FP 拆分失败 | `task.status=failed` → `return` 中断 |

**前端配合**：
- `Requirements.tsx` 轮询 `task.status`，`failed` 时展示红色错误框（含错误详情 + 重试按钮）
- `completed` 时检查 `result.degraded`，展示 3 秒橙色降级警告
- 错误标题动态化（红色"分析失败" / 橙色"分析完成（降级警告）"）

### 10.4 双 SRR 分层架构（SSOT 约束）

**设计**：总 SRR 只描述总体功能 + 跨功能点交互规则；FP sub_srr 描述单功能点内部细节，避免重复。

**实现**：
- `req_agent.py` Prompt 新增 SSOT 约束：总 SRR 不包含功能点级别细节
- 总 SRR 新增 `fp_index` 字段引用功能点（`_update_fp_index_in_srr()` 在所有变更点调用）
- `interaction_srr` 合并改为选择性合并：仅合并 `cross_fp_flows`/`shared_business_rules`/`fp_contracts`/`global_constraints`，不覆盖 `requirement`/`acceptance_criteria`
- FP 合并后代码级检测业务规则重复（FP sub_srr vs 总 SRR shared_business_rules）

### 10.5 异步任务进度反馈

**流程**（`upload_requirement_multi` → `_run_requirement_analysis`）：

```
POST /api/requirements/upload-multi
  → 立即创建 Requirement(analyzing) + AnalysisTask(pending)
  → 返回 {task_id, req_id}
  → 后台协程执行：
      phase=parse (10-30%)
      phase=kb_search (40%)
      phase=clarify (70%)
      phase=regenerate (85%) [仅有 KB 补全时]
      phase=done (100%)
  → 前端每 1.5s 轮询 GET /api/requirements/tasks/{task_id}
```

**任务状态机**：`pending → running → completed/failed/interrupted`

**进度阶段映射**（前端 `PHASE_MAP`）：
- `parse` → 阶段 0（解析需求文档）
- `kb_search` → 阶段 1（检索知识库）
- `clarify` → 阶段 2（需求澄清）
- `regenerate` → 阶段 3（生成最终分析）
- `done` → 阶段 4（完成）


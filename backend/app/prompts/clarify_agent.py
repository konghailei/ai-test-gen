"""ClarifyAgent 需求澄清提示词。

在最终 SRR 生成之前，对需求（原始文本 + 初步结构化草稿）进行三级检测
（完整性/歧义性/遗漏），识别会阻碍生成完整、可测试 SRR 的缺口与模糊点，
并生成针对 KB 无法解决之缺口的人工澄清问题。遵循 ClarifyGPT（ACL 2024）理念：
仅对模糊需求提问。

⚠️ 关键架构约定：本 Agent 运行在「最终 SRR 生成」之前，因此输入是
「需求 + 初步草稿（草稿 SRR）」，目标是找出「为生成完整 SRR 还缺什么」，
而非「审查已生成的 SRR」。提示词叙事必须与之一致。

设计依据：docs/01-requirement-parsing.md §4（ClarifyAgent 需求澄清闭环）
核心原则（docs/01 §4.1）：
    1. 仅对阻碍 SRR 完整生成的点提问 — 不浪费用户时间
    2. KB 优先 — 能自动补全的不问人工
    3. 问题精准 — 基于缺口分析，不问无关问题
    4. 多轮迭代 — 最多 3 轮，质量达标才放行
"""

from __future__ import annotations

import json

# ============================================================================
# 系统指令（歧义性检测 + 问题生成）
# ============================================================================

SYSTEM_PROMPT = """你是一个专业的软件需求澄清专家。在正式生成结构化需求（SRR）之前，你的任务是分析原始需求及其初步结构化草稿（草稿 SRR），识别会阻碍生成完整、可测试 SRR 的不清晰点与信息缺口，并仅对必须人工确认的点生成澄清问题。

## 工作流程

### 步骤 1：先确认，再提问（Confirm before asking）
仔细阅读原始需求文档全文和 SRR 草稿。对于每个检测维度（完整性/充分性/信息遗漏），先问自己：
- 该信息是否已在原始需求文档中明确描述？（措辞不同但语义相同也算） → 如果是，跳过
- 该信息是否已在 SRR 草稿中覆盖？ → 如果是，跳过
- 该信息能否从上下文合理推断？ → 如果能且推断可信，跳过

### 步骤 2：仅对确实阻塞的点提问（Only blocking questions）
- Critical：缺失会导致 SRR 主体无法生成 → 必须问
- Important：缺失会导致测试覆盖不全 → 应当问
- Optional：锦上添花 → 不问

## 核心原则
1. **宁缺勿滥**——只对确实会阻碍 SRR 完整生成的点提问。如果需求已经足够清晰、可直接生成完整 SRR，返回空问题列表 []
2. **原文为尊**——原始需求文档中已描述的信息不回退提问
3. **KB 优先**——能从知识库推断的信息不问人工
4. **问题精准**——每个问题必须明确说明为什么必须问

判断标准（问自己三个问题再决定是否提问）：
1. 该信息缺失是否会导致 SRR 主体无法生成？ → 是 → critical，必须问
2. 该信息缺失是否会导致 SRR 测试覆盖不全？ → 是 → important，应当问
3. 该信息只是锦上添花？ → optional，不问！

检测维度（四级，针对「生成完整、可测试 SRR 所需的信息」）：

1. 结构完整性检测：核心字段是否确实存在
   - Critical：requirement.summary（功能概述） / acceptance_criteria（验收标准）——完全缺失才问
   - Important：business_rules（核心业务规则） / flows.happy_path（正常流程） / flows.alternate_flows（异常/边界流程） / data_constraints（数据约束与校验规则）——完全缺失才问
   - Optional：不生成问题
   - 注意：字段存在（即使内容简短）即视为通过本维度，内容是否充分由维度2判断

2. 内容充分性检测：字段存在但内容空洞/过于简略，无法支撑测试用例设计
   - 例如：acceptance_criteria 只有「功能正常」之类空话，无具体、可验证的检查点
   - 例如：happy_path 只有一步「用户操作」，无具体操作步骤与系统响应
   - 例如：business_rules 只有「遵循业务规范」但无具体规则
   - 内容空洞 → 视为 important 缺口（category=completeness），应当追问具体内容

3. 业务语义遗漏检测：需求描述了功能场景，但应该存在却未提及的关键内容
   - 有正常流程但未提异常处理（如登录成功但未提密码错误、账号锁定、验证码、重试）
   - 有输入但未提输入校验规则（如格式、长度、特殊字符、必填性）
   - 有操作但未提权限要求（如谁可以执行、需要什么角色）
   - 有数据但未提数据约束（如字段类型、取值范围、唯一性）
   - 有流程但未提边界条件（如并发上限、超时、最大数量、分页）
   - 有输出但未提异常输出与错误码
   - 语义遗漏 → 视为 important 缺口（category=gap），应当追问

4. 歧义性与逻辑一致性检测：
   - 歧义性：仅对真正导致测试预期不确定的模糊表述提问（如「适当」「必要时」「视情况而定」「合理」），并说明会导致哪种测试不确定；category=ambiguity
   - 逻辑一致性：需求内部是否存在矛盾（同一字段不同位置描述不一致、流程步骤断裂、验收标准与功能描述矛盾）；category=conflict

输出要求：
- **如果需求足够清晰、可直接生成完整 SRR，返回空 questions 列表 []**
- 每个问题必须包含：id / field / priority / question / context / default_answer
- **category 取值**：completeness（结构/内容） / gap（语义遗漏） / ambiguity（歧义） / conflict（矛盾）
- **每个问题的 default_answer 不能为空**——必须基于需求上下文给出一个合理的预想答案
- **优先使用 input_type=select**，提供 2-4 个候选选项让用户选择，降低回答成本
- question 必须是完整问句，context 必须解释后果（缺失会导致 SRR 哪部分不完整、哪类测试无法覆盖）
- Critical/Important 问题全部展示，不限定数量；Optional 字段永远不生成问题

示例格式（default_answer 必填 + 尽量提供 options）：
{
  "id": "Q001",
  "field": "data_constraints.password.length",
  "priority": "important",
  "category": "completeness",
  "question": "密码长度范围是什么？",
  "context": "密码长度是边界值测试的必要参数，其缺口将导致 SRR 的 data_constraints 不完整、边界用例无法生成",
  "default_answer": "8-20位（常见安全规范）",
  "input_type": "select",
  "options": ["6-16位", "8-20位", "8-32位", "无限制"]
}"""


# ============================================================================
# 问题列表 Schema 说明
# ============================================================================

QUESTIONS_SCHEMA = """输出 JSON 必须符合以下结构：

{
  "issues_detected": [
    {
      "type": "completeness|ambiguity|gap|conflict",
      "severity": "critical|important|optional",
      "field": "字段路径，如 data_constraints.password.constraints",
      "description": "问题描述",
      "evidence": "证据，如原文引用或对比依据"
    }
  ],
  "questions": [
    {
      "id": "Q001",
      "field": "字段路径",
      "priority": "critical|important|optional",
      "category": "completeness|ambiguity|gap",
      "question": "针对该字段的具体问题（完整问句，含候选选项如适用）",
      "context": "为何需要澄清——影响哪个测试维度、缺失会导致什么后果",
      "default_answer": "建议的默认答案（供用户参考，降低回答成本）",
      "input_type": "text|select|multi_select",
      "options": ["选项A", "选项B"]
    }
  ],
  "quality_report": {
    "completeness_score": 0.0,
    "ambiguity_score": 0.0,
    "overall_score": 0.0,
    "summary": "整体质量评估说明"
  }
}

问题清晰度要求：
- question 必须是完整问句，明确询问什么信息（禁止简短如"请补充"）
- context 必须解释为何需要这个信息——影响哪个测试维度、缺失会导致什么后果
- default_answer 提供合理建议或默认值
- 如问题有明确选项，使用 input_type=select 并在 options 中给出候选

评分规则：
- completeness_score：1 - (critical缺失×3 + important缺失×1 + optional缺失×0.3) / 总字段数
- ambiguity_score：min(1.0, 歧义数 × 0.15)
- overall_score：completeness_score × 0.6 + (1 - ambiguity_score) × 0.4

问题数量：**Critical/Important 问题全部展示，不限定数量**——只要会实质影响 SRR 完整性与测试覆盖都应提问，宁缺勿滥。Optional 字段不生成问题。需求足够清晰、可直接生成完整 SRR 时返回空列表 []。"""


# ============================================================================
# 用户指令构建函数
# ============================================================================

def build_user_prompt(
    srr: dict,
    kb_inferred_fields: list[dict] | None = None,
    similar_reqs: list[dict] | None = None,
) -> str:
    """构建 ClarifyAgent 的用户指令。

    Args:
        srr: SRR JSON 字典
        kb_inferred_fields: KB 已自动补全的字段列表（这些不再生成问题），
            每项含 {field, value, similarity, source_req_id}
        similar_reqs: 同类历史需求列表（用于遗漏检测对比），
            每项含 {title, dimensions（缺失维度参考）}

    Returns:
        str: 完整的用户指令

    Note:
        - 将 KB 已补全的字段告知 LLM，避免重复提问
        - 将同类需求的维度列表注入，辅助遗漏检测
    """
    kb_hint = ""
    if kb_inferred_fields:
        kb_hint = f"""

【KB 已自动补全的字段（无需再问人工）】
{json.dumps(kb_inferred_fields, ensure_ascii=False, indent=2)}"""

    similar_hint = ""
    if similar_reqs:
        similar_hint = f"""

【同类历史需求参考（用于遗漏检测）】
{json.dumps(similar_reqs, ensure_ascii=False, indent=2)}"""

    return f"""请对以下需求（含初步结构化草稿）进行三级检测（完整性/歧义性/遗漏），识别会阻碍生成完整 SRR 的缺口与模糊点，并生成人工澄清问题列表。

{QUESTIONS_SCHEMA}
{kb_hint}
{similar_hint}

【待分析的需求与初步草稿】
{json.dumps(srr, ensure_ascii=False, indent=2)}

请直接输出检测结果 JSON，不要包含其他解释性文字。"""


# ============================================================================
# 模糊模式规则（供规则检测使用，非 LLM 调用）
# ============================================================================

AMBIGUITY_PATTERNS: dict[str, dict] = {
    "vague_quantifier": {
        "patterns": ["一些", "若干", "适当", "合理", "多个", "大量", "较快", "较慢"],
        "question_template": "「{original}」中的「{keyword}」具体指什么？请提供明确数值或范围。",
    },
    "vague_condition": {
        "patterns": ["必要时", "根据情况", "视情况而定", "如果需要", "特殊情况下"],
        "question_template": "「{original}」中「{keyword}」的具体触发条件是什么？请列举所有情况。",
    },
}

# ============================================================================
# KB 查询生成器 — 用 LLM 动态生成上下文感知的知识库搜索查询（v2.9）
# ============================================================================
# 替代 _formulate_kb_questions / _formulate_baseline_kb_questions 的固定模板。
# 固定模板（如「XX的正常操作流程是怎样的？」）不带具体场景上下文，
# 导致 ChromaDB 语义检索命中率下降，尤其对于增量文档、MFA/SSO 等细分场景。
# LLM 生成的查询包含功能名、关键特征、技术术语，检索更精准。

KB_QUERY_GENERATOR_SYSTEM_PROMPT = """你是一个知识库搜索查询生成器。你的核心职责是**过滤而非生成**——先严格检查每个缺口是否已被需求文档覆盖，只在确认真正缺失时才生成查询。

## 工作流程（严格按顺序执行）

### 步骤 1：逐条审阅缺口
对于「待补全的信息缺口」中的每一项，执行以下检查：

1. 这个缺口的关键词（流程名、规则类型、数据字段、场景描述）是否出现在「当期需求上下文」的任何部分？
2. 缺口的语义（不是说同样的词，而是说同一件事）是否已被上下文充分描述？

**判断标准：只要上下文中能找到对该缺口 70% 以上的覆盖，就判定为"已覆盖"，跳过。**

### 步骤 2：按覆盖结果分类
- **已覆盖** → 跳过，不生成查询
- **不确定是否覆盖** → 也跳过（不对未确认的事提问）
- **明确未覆盖** → 进入步骤 3

### 步骤 3：为明确未覆盖的缺口生成查询
- 每个真正缺失的缺口拆分为 1-3 条自然语言问句
- 每条以「？」结尾，8-25 字，含具体术语

## 核心原则
1. **过滤优先于生成**——你的首要任务是不让多余查询漏过去，而非生成更多查询
2. **不确定即跳过**——如果缺口是否已覆盖存在任何疑问，直接跳过
3. **只查明确缺失的**——查询内容必须是缺口描述中明确列举的维度
4. **不在缺口列表外添加任何主题**
5. 无缺口或全部已覆盖时返回空列表 []

## 输出格式
{
  "queries": [
    {
      "field": "flows.happy_path",
      "question": "密码在前端使用RSA公钥加密传输的完整流程是怎样的？",
      "priority": "important",
      "_is_baseline": false
    }
  ]
}

字段说明：
- field: 必须用输入中给定的 field 值，不能修改
- question: 自然语言问句，以「？」结尾
- priority: 用输入中给定的值
- _is_baseline: 仅缺口标记为基线补全时设为 true
- _gap_keys: 仅 _is_baseline 为 true 时设置，值固定为 ["happy_path","alternate_flows","data_constraints","business_rules","preconditions"]
"""

# ============================================================================
# KB 答案校验器 — LLM 判断检索结果是否真正相关（v2.11）
# ============================================================================
# 机械相似度过滤（≥0.65）无法判断语义相关性。例如：登录相关查询可能匹配到
# 注册/重置密码的文档，向量相似度 0.65 但内容完全无关。本步骤用 LLM 读取
# 检索到的文档内容，判断是否真正回答了查询、是否在当期需求范围内。

KB_ANSWER_VERIFIER_SYSTEM_PROMPT = """你是一个知识库答案质量审核员。判断知识库检索结果是否真正回答了查询问题，拒绝不相关的答案。

审核规则：
1. **内容相关性**：检索到的文档内容是否与查询直接相关？文档讨论的是不同主题则判定不相关
2. **范围约束**：文档内容是否在当期需求的范围内？当期需求是登录功能时，不应采纳注册、重置密码等无关内容
3. **信息充分性**：文档中有没有足够的具体信息？仅模糊描述（如"正常流程"）但无具体细节，判定为不充分

输出格式：
{
  "verifications": [
    {
      "question": "原始查询问题",
      "relevant": true,
      "extracted_answer": "从文档中提取的具体答案（relevant=true时必填）",
      "reason": "简短说明采纳或拒绝原因"
    }
  ]
}

判断原则：宁可拒绝模糊答案，也不采纳不相关信息。"""

COMPLETENESS_CHECKLIST: dict[str, dict[str, str]] = {
    # 仅保留对测试用例生成真正必需的核心字段
    "critical": {
        "requirement.summary": "功能概述",
        "acceptance_criteria": "验收标准",
    },
    "important": {
        "business_rules": "核心业务规则",
        "flows.happy_path": "正常流程",
        "flows.alternate_flows": "异常/边界流程",
        "data_constraints": "数据约束/校验规则",
    },
    # optional 级别不生成问题，仅用于评分
    "optional": {
        "preconditions": "前置条件",
        "non_functional.performance": "性能需求",
        "non_functional.security": "安全需求",
        "dependencies": "外部依赖",
    },
}

"""提示词模板包。

集中管理所有 Agent 的 LLM 提示词模板，实现提示词与业务逻辑解耦。
每个 Agent 对应一个提示词模块，提供系统指令、用户指令模板、Few-shot 示例。

模块清单：
- req_agent: ReqAgent 需求解析提示词（原始需求 → SRR JSON）
- clarify_agent: ClarifyAgent 需求澄清提示词（歧义检测 + 问题生成）
- impact_agent: ImpactAnalysisAgent 影响分析提示词（优化需求 → 变更影响 + 生成指导）
- gen_agent: GenAgent 用例生成提示词（SRR → UTCF 用例集）
- review_agent: ReviewAgent 用例审核提示词（UTCF → 审核报告 + 置信度）

设计依据：
- docs/01-requirement-parsing.md §3.1（ReqAgent 核心 Prompt）
- docs/01-requirement-parsing.md §4（ClarifyAgent 澄清流程）
- docs/03-case-generation-flow.md §1.5（ImpactAnalysisAgent）
- docs/03-case-generation-flow.md §2.2（GenAgent Few-shot Prompt 模板）
- docs/03-case-generation-flow.md §5.1（ReviewAgent 审核维度）
"""

from app.prompts import (
    clarify_agent,
    gen_agent,
    impact_agent,
    req_agent,
    review_agent,
)

__all__ = [
    "req_agent",
    "clarify_agent",
    "impact_agent",
    "gen_agent",
    "review_agent",
]

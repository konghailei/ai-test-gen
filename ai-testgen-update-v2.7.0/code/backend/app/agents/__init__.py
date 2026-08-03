"""Agent 模块。

集中管理 5 个核心 Agent，对应架构文档 docs/00 §4 的编排层：
    ReqAgent → ClarifyAgent → QualityGate → ImpactAnalysisAgent → GenAgent → ReviewAgent

模块清单：
- req_agent: ReqAgent 需求解析（原始需求 → SRR JSON）
- clarify_agent: ClarifyAgent 需求澄清（SRR → 澄清问题 + KB 补全 + 质量评分）
- impact_agent: ImpactAnalysisAgent 影响分析（优化需求 → 变更影响 + 生成指导）
- gen_agent: GenAgent 用例生成（SRR → UTCF 用例集）
- review_agent: ReviewAgent 用例审核（UTCF → 审核报告 + 最终置信度）

设计依据：
- docs/00-architecture-overview.md §4（Agent 编排层）
- docs/01-requirement-parsing.md §3-§5（ReqAgent / ClarifyAgent / QualityGate）
- docs/03-case-generation-flow.md §1-§5（ImpactAnalysisAgent / GenAgent / ReviewAgent）
"""

from app.agents import (
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

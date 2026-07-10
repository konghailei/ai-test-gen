"""通道选择器。

根据需求复杂度自动选择处理通道（FAST / FULL），实现"简单需求快速出结果、
复杂需求深度生成"的分级处理策略。

通道说明（docs/00-architecture-overview.md §9.1）：
- FAST（快速通道）：简单需求
    * 跳过 ClarifyAgent 澄清
    * 跳过 ImpactAnalysisAgent 影响分析
    * 单次批量生成（不分类并行）
    * 跳过 ReviewAgent 完整审核
    * 适用：P1/P2 优先级、happy_path ≤5 步、无优化上下文

- FULL（完整通道）：复杂需求
    * 启用 ClarifyAgent 澄清（最多 3 轮）
    * 启用 ImpactAnalysisAgent（仅优化需求触发）
    * 4 类型分类并行生成（功能/边界/异常/安全）
    * 启用 ReviewAgent 完整审核
    * 适用：P0 优先级、或 happy_path >5 步、或优化需求

设计依据：
- docs/00-architecture-overview.md §9.1（select_channel 函数规范）
- docs/00-architecture-overview.md §3（编排层 LangGraph DAG 入口节点调用）
- docs/01-requirement-parsing.md §7（optimization_context 优化需求上下文判断）
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# 通道类型
ChannelType = Literal["FAST", "FULL"]


class ChannelConfig(BaseModel):
    """通道配置。

    描述选定通道下各 Agent 的启用状态，供 LangGraph 编排器决定执行路径。
    对应 docs/00 §9.1 select_channel 的返回字典。
    """

    channel: ChannelType = Field(..., description="通道类型：FAST 或 FULL")
    enable_clarify: bool = Field(
        default=False, description="是否启用 ClarifyAgent 需求澄清"
    )
    enable_impact_analysis: bool = Field(
        default=False, description="是否启用 ImpactAnalysisAgent 影响分析（仅优化需求触发）"
    )
    enable_multi_type_generation: bool = Field(
        default=False,
        description="是否启用 4 类型分类并行生成；False 表示单次批量生成",
    )
    enable_full_review: bool = Field(
        default=False,
        description="是否启用 ReviewAgent 完整审核；False 表示采样审核（FAST 通道约 5%），"
                    "非完全跳过（docs/00 §9.1）",
    )

    @property
    def is_fast(self) -> bool:
        """是否为快速通道。"""
        return self.channel == "FAST"

    @property
    def is_full(self) -> bool:
        """是否为完整通道。"""
        return self.channel == "FULL"


# ============================================================================
# 通道选择核心函数
# ============================================================================

def select_channel(srr: dict) -> ChannelConfig:
    """根据需求复杂度选择处理通道。

    判定规则（docs/00 §9.1）：
        is_complex = (priority == "P0") or (len(happy_path.steps) > 5) or has_optimization_context

    - 复杂 → FULL 通道
    - 简单 → FAST 通道

    Args:
        srr: SRR JSON 字典。需包含 requirement.priority / flows.happy_path.steps /
            optimization_context 字段（缺失按默认值处理）

    Returns:
        ChannelConfig: 通道配置实例

    Note:
        - priority 实际位于 srr["requirement"]["priority"]，而非 srr["priority"]
          （修复说明 P2-6，docs/00 §9.1）
        - 优化需求通过 optimization_context 是否存在判断，SRR 中无 is_optimization 字段
        - 本函数为纯函数，无副作用，便于在 LangGraph 入口节点统一调用
    """
    priority = srr.get("requirement", {}).get("priority", "P2")
    happy_steps = srr.get("flows", {}).get("happy_path", {}).get("steps", []) or []
    has_opt_context = bool(srr.get("optimization_context"))

    # 复杂需求判定：P0 优先级 / happy_path 步骤 >5 / 优化需求
    is_complex = priority == "P0" or len(happy_steps) > 5 or has_opt_context

    if is_complex:
        # 复杂需求 → 完整通道
        return ChannelConfig(
            channel="FULL",
            enable_clarify=True,
            # 仅优化需求触发影响分析（非优化的 P0/复杂新需求不需要）
            enable_impact_analysis=has_opt_context,
            # 4 类型并行：功能/边界/异常/安全
            enable_multi_type_generation=True,
            enable_full_review=True,
        )
    else:
        # 简单需求 → 快速通道（MVP 链路）
        return ChannelConfig(
            channel="FAST",
            enable_clarify=False,
            enable_impact_analysis=False,
            # 单次批量生成，不分类并行
            enable_multi_type_generation=False,
            enable_full_review=False,
        )

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
    """选择处理通道——v1.6 全部走 FULL 通道，确保高质量生成。

    所有需求统一走 FULL 通道：
        - 启用 ClarifyAgent 需求澄清
        - 启用 ImpactAnalysisAgent 影响分析
        - 4 类型分类并行生成（功能/边界/异常/安全）
        - 启用 ReviewAgent 完整审核

    不再根据 priority/steps/FP 数量做 FAST/FAST 分级，
    确保每个需求都获得完整的用例生成流程，不再出现因通道选择错误
    导致只生成少量用例的问题（如 REQ-1784269856180-7617 只生成1个用例）。

    Args:
        srr: SRR JSON 字典（兼容原有参数签名，不再使用判定逻辑）

    Returns:
        ChannelConfig: FULL 通道配置
    """
    has_opt_context = bool(srr.get("optimization_context"))

    return ChannelConfig(
        channel="FULL",
        enable_clarify=True,
        # 仅优化需求触发影响分析
        enable_impact_analysis=has_opt_context,
        # 4 类型并行：功能/边界/异常/安全
        enable_multi_type_generation=True,
        enable_full_review=True,
    )

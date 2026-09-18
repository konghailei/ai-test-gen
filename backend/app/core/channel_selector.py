"""生成配置构造器。

关于"通道"的历史（v3.4.4 清理，勿再恢复分级）：
    本模块原名 `channel_selector`，实现「FAST / FULL 通道分级」——按 P1/P2 优先级、
    happy_path 步数判定走快速通道还是完整通道。该分级在 v1.6 被废止：曾因判成 FAST
    导致用例严重不足（如 REQ-1784269856180-7617 只生成 1 个用例），之后改为所有需求
    统一走完整流程，channel 恒为 FULL，判定逻辑成为死代码。

v3.4.4 按"去除冗余功能"原则清理：
    - 删除 `ChannelType` / `channel` 字段 / `is_fast` / `is_full`（FAST 已无任何实际路径，
      `req.channel` 前端传参也从不生效——generate.py 原本只记录日志）
    - 模块职责收敛为「构造本次生成的环节开关」

仍然有效的开关（均有真实消费方）：
    - `enable_impact_analysis`：仅优化需求启用影响分析，由 SRR 的 `optimization_context` 决定
    - `enable_multi_type_generation` / `enable_full_review`：恒 True，是质量保证的核心
      （当年只出 1 条用例，正是丢了 4 类型分类生成与完整审核）
    - `enable_clarify`：**在用例生成链路恒被置为 False**（见
      `orchestrator._select_channel_node`：已确认需求跳过生成期澄清）。
      字段保留是因为 DAG 的条件路由仍以它为准。

两条链路的边界（重要，避免误读）：
    - **需求分析链路**（`core/requirement_tasks.py`）**不经过本模块**，其澄清由
      `clarify_agent.clarify()` 独立执行，一直正常工作。
    - **用例生成链路**（`orchestrator.run_pipeline`）才使用本模块。
      因此"生成链路跳过澄清" 不等于 "系统关闭了澄清功能"。

设计依据：docs/00-architecture-overview.md §9.1（原 select_channel 规范）
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class GenerationConfig(BaseModel):
    """本次生成的环节开关（原 ChannelConfig）。

    描述本次生成启用哪些环节，供编排器决定执行路径。
    """

    enable_clarify: bool = Field(
        default=False,
        description="是否启用生成期澄清；已确认需求恒为 False（澄清已在需求分析链路完成）",
    )
    enable_impact_analysis: bool = Field(
        default=False, description="是否启用影响分析（仅优化需求触发）"
    )
    enable_multi_type_generation: bool = Field(
        default=True,
        description="是否启用 4 类型分类生成（功能/边界/异常/安全）；关闭会导致覆盖维度缺失",
    )
    enable_full_review: bool = Field(
        default=True, description="是否启用完整审核；关闭会退化为采样审核"
    )


def build_generation_config(srr: dict) -> GenerationConfig:
    """构造本次生成的环节开关。

    Args:
        srr: SRR JSON 字典，目前仅用于判断是否为优化需求

    Returns:
        GenerationConfig: 环节开关配置
    """
    return GenerationConfig(
        # 生成期澄清默认开启，由 orchestrator 在"已确认需求"场景改为 False
        enable_clarify=True,
        enable_impact_analysis=bool(srr.get("optimization_context")),
        enable_multi_type_generation=True,
        enable_full_review=True,
    )

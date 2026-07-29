"""核心组件包。

包含系统级核心组件：
- llm_client: LLM 客户端封装（故障转移 + 重试 + 断路器）
- quality_gate: 质量门禁（SRR 校验）
- channel_selector: 通道选择（FAST / FULL）
- orchestrator: LangGraph DAG 编排

设计依据：docs/00-architecture-overview.md §2 编排层 + 引擎层
"""

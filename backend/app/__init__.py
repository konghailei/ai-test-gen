"""AI-Test-Gen 后端应用包。

模块组织：
- app.api        API 路由层
- app.agents     5 个核心 Agent
- app.core       核心组件（LLM 客户端、质量门禁、通道选择、LangGraph）
- app.models     Pydantic 数据模型（SRR / UTCF）
- app.db         数据库层（SQLAlchemy + Milvus）
- app.exporters  导出器（Markdown / Gherkin / pytest）
- app.utils      工具函数
"""

__version__ = "1.1.0"

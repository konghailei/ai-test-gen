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

# 版本号唯一来源：同目录下的 VERSION 文件（纯文本，仅含版本字符串）
# pyproject.toml 通过 [tool.setuptools.dynamic].version = {file = ["backend/app/VERSION"]} 读取
# 前端 /health 接口下发的也是本值，全链路单一来源
def _read_version() -> str:
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "VERSION"), encoding="utf-8") as _f:
        return _f.read().strip()


__version__ = _read_version()

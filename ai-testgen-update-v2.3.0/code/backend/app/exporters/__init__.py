"""导出器模块。

将 UTCF 测试用例（list[dict]）转换为多种输出格式：
- Markdown：人类友好，用于 Review / Git diff / 文档归档（docs/04 §9）
- Gherkin：BDD 场景，用于 Cucumber 执行（docs/04 §3）
- pytest：Python 测试函数，用于自动化执行（docs/04 §4.1）

设计依据：
- docs/04-output-format.md §3（Gherkin 转换）
- docs/04-output-format.md §4.1（pytest 适配器）
- docs/04-output-format.md §9（Markdown 输出格式）

输入约定：
    导出器接收 list[dict]，每个 dict 是单条 UTCF TestCase 的 JSON 对象
    （对应 app.db.models.TestCase.case_json 字段，非完整 UTCF 包装）。
    导出器内部从用例集合推导 metadata（如 total_cases、coverage_summary）。

使用方式：
    from app.exporters import export_cases
    content, media_type, filename = export_cases(cases, fmt="markdown")
"""

from __future__ import annotations

from app.exporters.markdown import export_markdown
from app.exporters.gherkin import export_gherkin
from app.exporters.pytest_code import export_pytest
from app.exporters.xmind import export_to_xmind, export_to_xmind_xml
from app.utils.logger import logger


# ============================================================================
# 支持的导出格式
# ============================================================================

SUPPORTED_FORMATS: tuple[str, ...] = ("json", "markdown", "gherkin", "pytest", "xmind", "xmind8")

# 每种格式对应的 MIME 类型和文件后缀
_FORMAT_META: dict[str, tuple[str, str]] = {
    "json": ("application/json", "testcases.json"),
    "markdown": ("text/markdown; charset=utf-8", "testcases.md"),
    "gherkin": ("text/plain; charset=utf-8", "testcases.feature"),
    "pytest": ("text/x-python; charset=utf-8", "testcases.py"),
    "xmind": ("application/zip", "testcases.xmind"),
    "xmind8": ("application/zip", "testcases.xmind"),
}


# ============================================================================
# 工厂函数
# ============================================================================

def export_cases(cases: list[dict], fmt: str, project_name: str = "") -> tuple[str, str, str]:
    """导出用例为指定格式（工厂函数）。

    Args:
        cases: 用例 JSON 列表（每项为单条 UTCF TestCase dict）
        fmt: 导出格式，支持 json / markdown / gherkin / pytest

    Returns:
        tuple[str, str, str]: (content, media_type, filename)
            - content: 导出的文本内容
            - media_type: HTTP Content-Type
            - filename: 建议的下载文件名

    Raises:
        ValueError: 不支持的格式

    Note:
        - json 格式由 API 层直接处理（json.dumps），不经过此工厂
        - 其他三种格式由对应的导出器生成
    """
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"不支持的导出格式: {fmt}，支持: {SUPPORTED_FORMATS}")

    media_type, filename = _FORMAT_META[fmt]

    if fmt == "markdown":
        content = export_markdown(cases)
    elif fmt == "gherkin":
        content = export_gherkin(cases)
    elif fmt == "pytest":
        content = export_pytest(cases)
    elif fmt == "xmind":
        content = export_to_xmind(cases, project_name=project_name)
    elif fmt == "xmind8":
        content = export_to_xmind_xml(cases, project_name=project_name)
    else:
        # json 由 API 层处理，此处不会执行
        import json
        content = json.dumps(cases, ensure_ascii=False, indent=2)

    logger.info("[Exporters] 导出完成: format={}, cases={}", fmt, len(cases))
    return content, media_type, filename


__all__ = ["export_cases", "SUPPORTED_FORMATS"]

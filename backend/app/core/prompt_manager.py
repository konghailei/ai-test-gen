"""提示词管理器 — 集中管理所有 Agent 的提示词加载与覆盖。

约定：各 Agent 通过 `prompt_manager.get(module, name)` 获取提示词，
manager 优先返回 DB 中的用户自定义值，否则返回代码中的默认值。
"""

from __future__ import annotations

from app.db import prompt_store
from app.prompts import (
    req_agent,
    clarify_agent,
    gen_agent,
    review_agent,
    impact_agent,
    knowledge_qa,
)

# ============================================================================
# 提示词注册表 — 每个注册项定义一个默认可编辑的提示词
# ============================================================================

_PROMPT_REGISTRY: list[dict] = [
    # ReqAgent（需求解析）
    {
        "module": "req_agent",
        "name": "SYSTEM_PROMPT",
        "default": req_agent.SYSTEM_PROMPT,
        "description": "ReqAgent 系统指令 — 将原始需求解析为结构化 SRR",
    },
    {
        "module": "req_agent",
        "name": "FEW_SHOT_EXAMPLE",
        "default": req_agent.FEW_SHOT_EXAMPLE,
        "description": "ReqAgent Few-shot 示例 — 登录功能 SRR 范式",
    },
    # ClarifyAgent（需求澄清）
    {
        "module": "clarify_agent",
        "name": "SYSTEM_PROMPT",
        "default": clarify_agent.SYSTEM_PROMPT,
        "description": "ClarifyAgent 系统指令 — 需求完整性与歧义检测",
    },
    {
        "module": "clarify_agent",
        "name": "UNRESOLVED_REFINER_SYSTEM_PROMPT",
        "default": clarify_agent.UNRESOLVED_REFINER_SYSTEM_PROMPT,
        "description": "ClarifyAgent 未答缺口精炼 — 为 KB 无法回答的缺口生成带可选项的澄清问题",
    },
    # GenAgent（用例生成）
    {
        "module": "gen_agent",
        "name": "SYSTEM_PROMPT",
        "default": gen_agent.SYSTEM_PROMPT,
        "description": "GenAgent 系统指令 — 资深测试工程师生成 UTCF 用例",
    },
    # ReviewAgent（用例审核）
    {
        "module": "review_agent",
        "name": "SYSTEM_PROMPT",
        "default": review_agent.SYSTEM_PROMPT,
        "description": "ReviewAgent 系统指令 — 6 维度用例审核",
    },
    # ImpactAnalysisAgent（影响分析）
    {
        "module": "impact_agent",
        "name": "SYSTEM_PROMPT",
        "default": impact_agent.SYSTEM_PROMPT,
        "description": "ImpactAnalysis 系统指令 — 变更影响分析",
    },
    # ClarifyAgent KB 相关提示词（v2.12 补充注册）
    {
        "module": "clarify_agent",
        "name": "KB_QUERY_GENERATOR_SYSTEM_PROMPT",
        "default": clarify_agent.KB_QUERY_GENERATOR_SYSTEM_PROMPT,
        "description": "ClarifyAgent KB 查询生成器 — LLM 动态生成上下文感知的搜索查询",
    },
    {
        "module": "clarify_agent",
        "name": "KB_ANSWER_VERIFIER_SYSTEM_PROMPT",
        "default": clarify_agent.KB_ANSWER_VERIFIER_SYSTEM_PROMPT,
        "description": "ClarifyAgent KB 答案校验器 — LLM 审核检索结果的相关性与准确性",
    },
    # KnowledgeQA（知识问答）
    {
        "module": "knowledge_qa",
        "name": "QA_SYSTEM_PROMPT",
        "default": knowledge_qa.QA_SYSTEM_PROMPT,
        "description": "知识问答系统指令 — 基于知识库上下文回答用户问题",
    },
    {
        "module": "knowledge_qa",
        "name": "CHAT_SYSTEM_PROMPT",
        "default": knowledge_qa.CHAT_SYSTEM_PROMPT,
        "description": "知识问答追问系统指令 — 多轮对话中的总结/提炼/修改",
    },
]


def _init_registry():
    """将注册表中的默认值写入 prompt_store（仅首次或默认值变更时更新）。"""
    for item in _PROMPT_REGISTRY:
        prompt_store.register_default(
            module=item["module"],
            name=item["name"],
            default_value=item["default"],
            description=item["description"],
        )


# ============================================================================
# 公开 API
# ============================================================================


def get(module: str, name: str) -> str:
    """获取提示词：优先 DB 覆盖值，否则返回代码默认值。

    用法：
        sys_prompt = prompt_manager.get("req_agent", "SYSTEM_PROMPT")
    """
    # 确保 registry 已初始化
    _init_registry()

    override = prompt_store.get_prompt(module, name)
    if override is not None:
        return override

    # 回退到默认值
    return prompt_store.get_prompt_default(module, name) or ""


def load_all_overrides() -> dict[str, str]:
    """加载所有覆盖的提示词，返回 {module.name: value}。"""
    _init_registry()
    result = {}
    for item in _PROMPT_REGISTRY:
        val = prompt_store.get_prompt(item["module"], item["name"])
        if val is not None and val != item["default"]:
            result[f"{item['module']}.{item['name']}"] = val
    return result


def list_prompts() -> list[dict]:
    """列出所有提示词（供 API 返回）。"""
    _init_registry()
    return prompt_store.get_all_prompts()


def update(module: str, name: str, value: str) -> None:
    """更新提示词（保存用户自定义）。"""
    prompt_store.update_prompt(module, name, value)


def reset(module: str, name: str) -> None:
    """恢复为默认值。"""
    prompt_store.reset_prompt(module, name)


# 首次导入时自动初始化
_init_registry()

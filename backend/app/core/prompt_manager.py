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
    knowledge_qa,
    fp_agent,
    vision,
    prompt_evolution,
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
    # v2.8.67: 重新注册 SYSTEM_PROMPT--v2.8.56 曾以"_generate_questions 未被主流程调用"
    # 为由移除注册，但该判断有误（_generate_questions 在 clarify() L669 确实被调用），
    # 导致 DB 中残留的旧版 SYSTEM_PROMPT（3981字）长期覆盖代码里持续迭代的最新版
    # （含 v2.8.65 引用未定义/跨节矛盾专项 + v2.8.67 设计阶段细节抑制）。
    # 重新注册后，register_default 会因 value==default 将 DB 的 value/default 同步为最新代码值。
    # v3.1.5: 按用户要求精简——原 6 个提示词收敛为 2 个：
    #   保留 SYSTEM_PROMPT（问题生成，含字段冲突检测+每题必带 options）
    #   保留 KB_ANSWER_VERIFIER_SYSTEM_PROMPT（KB 答案校验）
    #   砍掉 KB_QUERY_GENERATOR / UNRESOLVED_REFINER / FIELD_CONFLICT_RESOLVER /
    #   OPTIMIZATION_DETECTOR 四个重复轮子（对应逻辑改回规则实现，见 clarify_agent.py）。
    {
        "module": "clarify_agent",
        "name": "SYSTEM_PROMPT",
        "default": clarify_agent.SYSTEM_PROMPT,
        "description": "ClarifyAgent 系统指令 - LLM 语义检测生成澄清问题（完整性/充分性/遗漏/歧义/矛盾/引用未定义，每题必带备选答案）",
    },
    # GenAgent（用例生成）
    {
        "module": "gen_agent",
        "name": "SYSTEM_PROMPT",
        "default": gen_agent.SYSTEM_PROMPT,
        "description": "GenAgent 系统指令 — 资深测试工程师生成 UTCF 用例",
    },
    {
        "module": "gen_agent",
        "name": "DEDUP_EQUIVALENCE_SYSTEM_PROMPT",
        "default": gen_agent.DEDUP_EQUIVALENCE_SYSTEM_PROMPT,
        "description": "GenAgent 语义去重 — 机械相似度预筛后，LLM 判定用例对是否测试同一场景（v3.1.7 从内联提取注册）",
    },
    # ReviewAgent（用例审核）
    {
        "module": "review_agent",
        "name": "SYSTEM_PROMPT",
        "default": review_agent.SYSTEM_PROMPT,
        "description": "ReviewAgent 系统指令 — 6 维度用例审核",
    },
    # v3.1.7: ImpactAnalysisAgent 的 SYSTEM_PROMPT 注册项已删除——
    # 影响分析为纯规则实现（图谱 BFS + diff 规划），该提示词从未被任何代码引用（孤儿）；
    # FP 影响说明也改为规则拼接（LLM 仅在复读图谱输出，且有编造依赖的风险）。
    # ClarifyAgent KB 相关提示词（v2.12 补充注册；v3.1.5 仅保留答案校验器）
    {
        "module": "clarify_agent",
        "name": "KB_ANSWER_VERIFIER_SYSTEM_PROMPT",
        "default": clarify_agent.KB_ANSWER_VERIFIER_SYSTEM_PROMPT,
        "description": "ClarifyAgent KB 答案校验器 — LLM 审核检索结果的相关性与准确性",
    },
    {
        "module": "clarify_agent",
        "name": "CLARIFY_COVERAGE_EVAL_SYSTEM_PROMPT",
        "default": clarify_agent.CLARIFY_COVERAGE_EVAL_SYSTEM_PROMPT,
        "description": "ClarifyAgent 收敛评估 — 多轮澄清时判断用户回答是否已覆盖问题核心（覆盖则收敛，未覆盖生成追问）",
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
    # FP（功能点拆分，v1.4）
    {
        "module": "fp_agent",
        "name": "FP_INTEGRATED_SYSTEM_PROMPT",
        "default": fp_agent.FP_INTEGRATED_SYSTEM_PROMPT,
        "description": "FP-Agent 一体式拆分 — 原始文档 + SRR → FP 列表 + 子 SRR",
    },
    {
        "module": "fp_agent",
        "name": "FP_INTEGRATED_USER_PROMPT",
        "default": fp_agent.FP_INTEGRATED_USER_PROMPT,
        "description": "FP-Agent 一体式拆分用户提示词模板",
    },
    # Vision（多模态图片识别，v2.18 新增）
    {
        "module": "vision",
        "name": "SYSTEM_PROMPT",
        "default": vision.SYSTEM_PROMPT,
        "description": "多模态图片识别系统指令 — 流程图/架构图/状态图等图片的结构化描述",
    },
    {
        "module": "vision",
        "name": "USER_PROMPT_TEMPLATE",
        "default": vision.USER_PROMPT_TEMPLATE,
        "description": "多模态图片识别用户提示词模板 — 图片识别请求模板",
    },
    # v4.2.4：以下四项原本是「管理盲区」——要么硬编码在 core/（PromptEvolution 的系统提示词），
    # 要么作为静态常量直接拼进 User Prompt，管理台看不到、也就无法跟着评审意见演进。
    # 现已注册，并在各自的 build_user_prompt 里通过 prompt_manager 读取（局部导入避免循环依赖）。
    {
        "module": "prompt_evolution",
        "name": "INTEGRATION_SYSTEM_PROMPT",
        "default": prompt_evolution.INTEGRATION_SYSTEM_PROMPT,
        "description": "PromptEvolution 系统指令 — 把评审采纳意见合并为「历史评审沉淀规则」区段",
    },
    {
        "module": "gen_agent",
        "name": "UTCF_CASE_SCHEMA",
        "default": gen_agent.UTCF_CASE_SCHEMA,
        "description": "用例 JSON 结构说明 — 由 build_user_prompt 拼入 User Prompt（与 SYSTEM_PROMPT 的 few-shot 互补）",
    },
    {
        "module": "clarify_agent",
        "name": "QUESTIONS_SCHEMA",
        "default": clarify_agent.QUESTIONS_SCHEMA,
        "description": "澄清问题 JSON 结构 — 拼入 User Prompt，与 SYSTEM_PROMPT 里的示例互补",
    },
    {
        "module": "req_agent",
        "name": "SRR_SCHEMA_GUIDE",
        "default": req_agent.SRR_SCHEMA_GUIDE,
        "description": "SRR 输出结构指南 — 拼入需求解析 User Prompt",
    },
]


# v3.4.1 修复 M-13：进程内缓存与 registry 初始化标记
_registry_initialized = False
_prompt_cache: dict[str, str] = {}


def _init_registry(force: bool = False):
    """将注册表中的默认值写入 prompt_store（默认只在首次执行）。

    v3.4.1 修复 M-13：原实现无任何守卫，每次 get() 都全量 upsert 14 项
    （每项 1 次 information_schema 查询 + 1 次 SELECT + 1 次 COMMIT），
    而 get() 在每次 LLM 调用前都会触发，全部同步阻塞在 async 事件循环里。
    现在只在首次导入（或显式 force=True）时执行。
    """
    global _registry_initialized
    if _registry_initialized and not force:
        return
    for item in _PROMPT_REGISTRY:
        prompt_store.register_default(
            module=item["module"],
            name=item["name"],
            default_value=item["default"],
            description=item["description"],
        )
    _registry_initialized = True


def invalidate_cache(module: str | None = None, name: str | None = None) -> None:
    """失效进程内提示词缓存（写路径调用；不传参数则清空全部）。

    v3.4.1 修复 M-13：get() 现在带进程内缓存，提示词被修改后必须失效，
    否则当前进程会一直返回旧值。单实例部署下由本函数保证一致性；
    多实例部署时其他实例依赖其自身缓存（改提示词后需重启或后续加跨进程通知）。
    """
    if module is None or name is None:
        _prompt_cache.clear()
        return
    _prompt_cache.pop(f"{module}.{name}", None)


# ============================================================================
# 公开 API
# ============================================================================


def get(module: str, name: str) -> str:
    """获取提示词：优先 DB 覆盖值，否则返回代码默认值。

    用法：
        sys_prompt = prompt_manager.get("req_agent", "SYSTEM_PROMPT")
    """
    # v3.4.1 修复 M-13：进程内缓存命中直接返回，避免每次 LLM 调用前的同步 DB 往返
    key = f"{module}.{name}"
    cached = _prompt_cache.get(key)
    if cached is not None:
        return cached

    # 确保 registry 已初始化（有 flag 短路，仅首次真正执行 DB 写入）
    _init_registry()

    override = prompt_store.get_prompt(module, name)
    if override is not None:
        _prompt_cache[key] = override
        return override

    # 回退到默认值（同样写缓存，含空串）
    value = prompt_store.get_prompt_default(module, name) or ""
    _prompt_cache[key] = value
    return value


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


def update(module: str, name: str, value: str, changed_by: str = "api") -> None:
    """更新提示词（保存用户自定义）。

    v2.3: 自动写入 prompt_history 表记录修改历史。
    """
    prompt_store.update_prompt(module, name, value, changed_by=changed_by)
    # v3.4.1 修复 M-13：写路径失效进程内缓存
    invalidate_cache(module, name)


def reset(module: str, name: str, changed_by: str = "api") -> None:
    """恢复为默认值。

    v2.3: 自动写入 prompt_history 表记录恢复操作。
    """
    prompt_store.reset_prompt(module, name, changed_by=changed_by)
    # v3.4.1 修复 M-13：写路径失效进程内缓存
    invalidate_cache(module, name)


def list_history(module: str, name: str, limit: int = 50) -> list[dict]:
    """获取提示词修改历史列表（v2.3 新增）。"""
    return prompt_store.list_prompt_history(module, name, limit=limit)


def restore_from_history(history_id: int, changed_by: str = "api") -> dict:
    """从历史版本恢复提示词（v2.3 新增）。

    Args:
        history_id: 历史记录 ID
        changed_by: 操作人

    Returns:
        dict: 含恢复后的提示词信息
    """
    result = prompt_store.restore_prompt_from_history(history_id, changed_by=changed_by)
    # v3.4.1 修复 M-13：不确定恢复的是哪一条，整体失效进程内缓存
    invalidate_cache()
    return result


# 首次导入时自动初始化
_init_registry()

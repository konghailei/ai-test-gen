"""GenAgent 用例生成 Agent。

基于 SRR 生成完整的 UTCF 测试用例集，支持分类生成策略
（功能/边界/异常/安全）和 Few-shot 示例注入。

设计依据：
- docs/03-case-generation-flow.md §2（GenAgent 核心设计）
- docs/03-case-generation-flow.md §2.2（Few-shot Prompt 模板）

分类生成策略（docs/03 §2.1）：
    - 功能测试：Step-by-step 推导，SRR.flows → Given-When-Then
    - 边界值测试：等价类 + 边界分析，SRR.data_constraints → 边界
    - 异常测试：故障注入 + 防御推理，枚举故障点 + 预期处理
    - 安全测试：OWASP 检查清单，SQL注入/XSS/越权/脱敏

输入：SRR JSON + 通道配置 + 影响指导（可选）
输出：UTCF 用例列表

Note:
    - gen_confidence 必须放在 gen_metadata 对象内（修复问题5）
    - 用例 ID 格式：TC-{MODULE}-{4位批次}-{4位序号}（符合 doc-04 §1 正则）
    - FULL 通道启用分类生成，FAST 通道单次批量生成
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from app.config import settings
from app.core.llm_client import llm_client
from app.core import prompt_manager
from app.db.milvus_store import vector_store
from app.prompts import gen_agent as prompts
from app.utils.id_gen import gen_test_case_id, gen_batch_id
from app.utils.logger import logger


class GenAgent:
    """用例生成 Agent。

    对应 docs/00-architecture-overview.md §4 的 GenAgent 节点。

    Usage:
        agent = GenAgent()
        cases = await agent.generate(srr, channel_config, impact_guidance="")
    """

    def __init__(self) -> None:
        """初始化 GenAgent。"""
        self._llm = llm_client

    async def generate(
        self,
        srr: dict,
        enable_multi_type: bool = True,
        impact_guidance: str = "",
        functional_points: list[dict] | None = None,
    ) -> list[dict]:
        """根据 SRR 生成测试用例集。

        Args:
            srr: SRR JSON 字典
            enable_multi_type: 是否启用分类生成（FULL 通道 True / FAST 通道 False）
            impact_guidance: 影响分析指导文本（仅优化需求时传入）
            functional_points: 需求已拆分的功能点列表（v1.4 新增）。传入时
                LLM 按每个 FP 定向生成用例，并在用例中标注 related_fp 字段，
                供 persist_to_kb 写入 test_case↔functional_point 追溯关系。

        Returns:
            list[dict]: UTCF 用例列表，每个用例为字典格式

        Note:
            生成流程（docs/03 §2，v3.1 起跳过 KB 检索）：
                1. 构建 Prompt（分类/批量 + FP 定向，直接基于 SRR）
                2. 调用 LLM 生成
                3. 解析用例 JSON + 生成 ID
                4. gen_confidence 放入 gen_metadata
        """
        req_id = srr.get("requirement", {}).get("id", "")
        logger.info("[GenAgent] ▶ 开始生成: req_id={}, multi_type={}, has_impact_guidance={}, fp_count={}",
                    req_id, enable_multi_type, bool(impact_guidance), len(functional_points or []))

        # v3.1: 跳过 KB 检索，直接基于 SRR 生成（SRR 已足够详细，需求上下文在澄清阶段已由知识库补全）
        few_shot_cases: list = []
        matched_patterns: list = []
        functional_points = functional_points or []
        logger.info("[GenAgent] KB 检索已跳过（直接基于 SRR 生成）: few_shot_cases={}, matched_patterns={}",
                    len(few_shot_cases), len(matched_patterns))

        # 3. 构建 Prompt（含 FP 定向上下文）
        user_prompt = prompts.build_user_prompt(
            srr=srr,
            few_shot_cases=few_shot_cases or None,
            matched_patterns=matched_patterns or None,
            impact_guidance=impact_guidance,
            enable_multi_type=enable_multi_type,
            functional_points=functional_points,
        )
        logger.debug("[GenAgent] Prompt 构建完成: len={}", len(user_prompt))

        # 4. 调用 LLM 生成用例（max_tokens=32768，确保能生成足够的用例）
        try:
            logger.info("[GenAgent] 调用 LLM: temperature=0.4, multi_type={}, fp_count={}, "
                        "max_tokens=32768",
                        enable_multi_type, len(functional_points or []))
            raw_cases = await self._llm.call_json(
                prompt=user_prompt,
                system_prompt=prompt_manager.get("gen_agent", "SYSTEM_PROMPT"),
                temperature=0.4,
                max_tokens=32768,
                allow_array=True,  # 允许 LLM 输出 JSON 数组，不锁死 json_object
            )
        except Exception as e:
            logger.error("[GenAgent] LLM 调用失败: {}", str(e)[:200])
            raise RuntimeError(f"用例生成失败: {e}") from e

        # 5. 解析用例列表（LLM 可能返回数组或含 test_suite 的对象）
        cases = self._extract_cases(raw_cases)
        logger.info("[GenAgent] LLM 返回用例: raw_type={}, extracted={}",
                    type(raw_cases).__name__, len(cases))

        # 6. 后处理：生成 ID、补全字段、放入 gen_metadata
        module_prefix = self._derive_module_prefix(srr)
        batch_id = gen_batch_id()
        gen_model = self._get_model_name()
        cases = self._post_process_cases(cases, req_id, module_prefix, batch_id, gen_model)
        # 统计用例类型分布
        type_dist: dict[str, int] = {}
        for c in cases:
            t = c.get("type", "unknown")
            type_dist[t] = type_dist.get(t, 0) + 1
        logger.info("[GenAgent] 后处理完成: cases={}, module_prefix={}, type_dist={}",
                    len(cases), module_prefix, type_dist)

        # 7. 写入向量库（优化 1：并发写入，N 个用例 asyncio.gather 并发执行）
        product_id = srr.get("requirement", {}).get("product_id", "")

        async def _write_one_case(case: dict) -> bool:
            """并发写入单个用例向量，返回是否成功。"""
            try:
                await asyncio.to_thread(
                    vector_store.add_test_case,
                    case_id=case["id"],
                    case_json=case,
                    metadata={
                        "req_id": req_id,
                        "product_id": product_id,
                        "type": case.get("type", ""),
                        "priority": case.get("priority", ""),
                    },
                )
                return True
            except Exception as e:
                logger.debug("[GenAgent] 向量库写入失败（不阻断）: case_id={}, err={}",
                             case.get("id", ""), str(e)[:120])
                return False

        import time as _time
        _vb_t0 = _time.time()
        results = await asyncio.gather(*[_write_one_case(c) for c in cases])
        vb_success = sum(1 for r in results if r)
        vb_fail = sum(1 for r in results if not r)
        _vb_duration = int((_time.time() - _vb_t0) * 1000)
        if vb_fail > 0:
            logger.warning("[GenAgent] 向量库写入: 成功={}, 失败={}, 耗时={}ms",
                           vb_success, vb_fail, _vb_duration)
        else:
            logger.info("[GenAgent] 向量库写入完成: 成功={}, 耗时={}ms", vb_success, _vb_duration)

        logger.info("[GenAgent] ◀ 生成完成 ▶ 下一节点 ReviewAgent: "
                    "cases={}, types={}, batch_id={}",
                    len(cases), type_dist, batch_id)
        return cases

    # ========================================================================
    # 用例解析与后处理
    # ========================================================================

    def _extract_cases(self, raw: Any) -> list[dict]:
        """从 LLM 输出中提取用例列表。

        LLM 可能返回：
        - JSON 数组 [...]
        - 含 test_suite 的对象 {"test_suite": [...]}
        - 单个用例对象 {...}

        Args:
            raw: LLM 输出

        Returns:
            list[dict]: 用例列表
        """
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            if "test_suite" in raw:
                return raw["test_suite"]
            if "cases" in raw:
                return raw["cases"]
            # 单个用例
            if "title" in raw or "steps" in raw:
                return [raw]
        return []

    def _post_process_cases(
        self,
        cases: list[dict],
        req_id: str,
        module_prefix: str,
        batch_id: str,
        gen_model: str,
    ) -> list[dict]:
        """后处理：生成 ID、补全字段、整理 gen_metadata。

        Args:
            cases: LLM 生成的原始用例列表
            req_id: 关联需求 ID
            module_prefix: 模块前缀（用于生成用例 ID）
            batch_id: 生成批次 ID
            gen_model: 生成模型名

        Returns:
            list[dict]: 后处理后的用例列表

        Note:
            - gen_confidence 必须放在 gen_metadata 对象内（修复问题5）
            - 用例 ID 缺失则自动生成
            - related_requirement 缺失则补 req_id
        """
        now = datetime.now().isoformat()
        processed: list[dict] = []

        for case in cases:
            # ⚠️ 始终覆盖 LLM 生成的占位 ID（如 TC-AUTH-0001-0001），
            # 避免同模块重复生成时 UniqueViolationError
            case["id"] = gen_test_case_id(module_prefix)

            # 补全关联需求
            case.setdefault("related_requirement", req_id)

            # 补全必填字段
            case.setdefault("title", "未命名用例")
            case.setdefault("type", "functional")
            case.setdefault("priority", "P1")
            case.setdefault("preconditions", [])
            case.setdefault("steps", [])
            case.setdefault("assertions", [])
            case.setdefault("tags", [])
            case.setdefault("test_data", {})
            case.setdefault("cleanup", [])
            case.setdefault("execution", {})

            # ⚠️ gen_confidence 必须放在 gen_metadata 内（修复问题5）
            # 如果 LLM 把 gen_confidence 放在了顶层，移入 gen_metadata
            top_level_conf = case.pop("gen_confidence", None)
            gen_metadata = case.setdefault("gen_metadata", {})
            if top_level_conf is not None:
                gen_metadata.setdefault("gen_confidence", top_level_conf)
            gen_metadata.setdefault("gen_confidence", 0.7)  # 默认置信度
            gen_metadata.setdefault("gen_model", gen_model)
            gen_metadata.setdefault("gen_at", now)

            # source_pattern 同步到 gen_metadata 和顶层
            if "source_pattern" in case:
                gen_metadata.setdefault("source_pattern", case["source_pattern"])
            else:
                case.setdefault("source_pattern", "")

            processed.append(case)

        return processed

    def _derive_module_prefix(self, srr: dict) -> str:
        """从 SRR 推导用例 ID 的模块前缀。

        Args:
            srr: SRR JSON 字典

        Returns:
            str: 模块前缀（大写字母），如 AUTH / CRUD
        """
        modules = srr.get("requirement", {}).get("modules", [])
        if modules:
            # 取第一个模块名，提取字母部分
            prefix = "".join(c for c in modules[0].upper() if c.isalpha())
            return prefix[:10] or "GEN"
        return "GEN"

    def _get_model_name(self) -> str:
        """获取当前 LLM 模型名。"""
        from app.config import settings
        return settings.llm_config.get("model", "unknown")


# 全局单例
gen_agent = GenAgent()

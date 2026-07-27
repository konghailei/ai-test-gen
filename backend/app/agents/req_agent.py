"""ReqAgent 需求解析 Agent。

将原始需求文本（PRD/JIRA/用户故事/纯文本）解析为结构化 SRR JSON。
采用 CoT（Chain-of-Thought）分步提取策略，确保字段完整。

设计依据：
- docs/01-requirement-parsing.md §3（ReqAgent 解析流程）
- docs/01-requirement-parsing.md §3.1（核心 Prompt）

解析流程：
    ① 格式自动检测 → ② 预处理 → ③ LLM 结构化解析 →
    ④ 字段校验 → ⑤ 实体链接 → ⑥ 测试点提示生成 → SRR JSON

输入：原始需求文本 + 产品信息
输出：SRR JSON 字典（符合 SRR Pydantic 模型）
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.core.llm_client import llm_client
from app.db.milvus_store import vector_store
from app.models.srr import SRR
from app.prompts import req_agent as prompts
from app.core import prompt_manager
from app.utils.id_gen import gen_requirement_id
from app.utils.logger import logger


class ReqAgent:
    """需求解析 Agent。

    将非结构化需求文本转为结构化 SRR JSON。
    对应 docs/00-architecture-overview.md §4 的 ReqAgent 节点。

    Usage:
        agent = ReqAgent()
        srr = await agent.parse("用户登录功能：账号密码登录...", "PROD-001", "用户中心")
    """

    def __init__(self) -> None:
        """初始化 ReqAgent。"""
        self._llm = llm_client
        self.last_fps: list[dict] = []  # v1.5.1: 存储最近一次解析提取的功能点
        self.last_fp_dependencies: list[dict] = []  # v1.5.1: FP 间依赖关系
        self.interaction_srr: dict | None = None  # v1.6: 交互层 SRR（跨 FP 规则）

    async def parse(
        self,
        raw_requirement: str,
        product_id: str = "",
        product_name: str = "",
    ) -> dict:
        """解析原始需求为 SRR JSON。

        Args:
            raw_requirement: 原始需求文本（Markdown/JSON/JIRA/纯文本）
            product_id: 产品 ID（可选，由调用方传入则注入到 SRR）
            product_name: 产品名称（可选）

        Returns:
            dict: SRR JSON 字典，符合 SRR Pydantic 模型

        Raises:
            RuntimeError: LLM 调用失败或解析结果不符合 Schema

        Note:
            解析流程（docs/01 §3）：
                1. 构建 Prompt（注入 SRR Schema 指导 + Few-shot 示例）
                2. 调用 LLM（call_json，强制 JSON 输出，温度 0.5 保提取质量）
                3. 后处理：字段归一化 + 补全 + Literal 映射
                4. Schema 宽松校验（容错模式，仅 Critical 字段阻断）
                5. 写入向量库（供后续 ClarifyAgent 检索）
        """
        logger.info("[ReqAgent] ▶ 开始解析需求: text_len={}, product_id={}, product_name={}",
                    len(raw_requirement), product_id or "(未指定)", product_name or "(未指定)")

        try:
            return await self._parse_impl(raw_requirement, product_id, product_name)
        except Exception as e:
            import traceback
            logger.error("[ReqAgent] ❌ 解析异常: {}\n{}", str(e)[:500], traceback.format_exc()[-600:])
            raise

    async def refine_srr(
        self,
        current_srr: dict,
        extra_notes: str = "",
        product_id: str = "",
        product_name: str = "",
    ) -> dict:
        """基于现有 SRR + 补充说明修改生成新 SRR。

        v2.20: 重新生成不再从原始文件解析，而是基于现有 SRR 修改。
        复用 _parse_impl 的后处理、校验、向量库写入、FP 提取逻辑。

        Args:
            current_srr: 当前 SRR JSON 字典
            extra_notes: 用户补充修改说明
            product_id: 产品 ID（可选）
            product_name: 产品名称（可选）

        Returns:
            dict: 修改后的 SRR JSON 字典
        """
        logger.info("[ReqAgent] ▶ 开始基于现有 SRR 修改: srr_keys={}, notes_len={}, product_id={}",
                    list(current_srr.keys()) if isinstance(current_srr, dict) else "N/A",
                    len(extra_notes), product_id or "(未指定)")

        try:
            return await self._refine_impl(current_srr, extra_notes, product_id, product_name)
        except Exception as e:
            import traceback
            logger.error("[ReqAgent] ❌ SRR 修改异常: {}\n{}", str(e)[:500], traceback.format_exc()[-600:])
            raise

    async def _refine_impl(
        self,
        current_srr: dict,
        extra_notes: str,
        product_id: str,
        product_name: str,
    ) -> dict:
        """基于现有 SRR 修改的实现（内部）。

        复用 _parse_impl 的后处理逻辑，但使用 refine prompt。
        """
        # 1. 构建 refine prompt
        user_prompt = prompts.build_refine_prompt(current_srr, extra_notes, product_id, product_name)
        system_prompt = prompt_manager.get("req_agent", "SYSTEM_PROMPT") + "\n\n" + prompt_manager.get("req_agent", "FEW_SHOT_EXAMPLE")
        logger.debug("[ReqAgent] Refine Prompt 构建完成: user_prompt_len={}, system_prompt_len={}",
                     len(user_prompt), len(system_prompt))

        # 2. 调用 LLM（强制 JSON 输出）
        schema_hint = {
            "srr_version": str,
            "requirement": dict,
            "flows": dict,
            "data_constraints": list,
            "acceptance_criteria": list,
            "api_definitions": list,
            "additional_details": str,
        }
        logger.info("[ReqAgent] 调用 LLM (refine): temperature=0.5, max_tokens=unlimited")
        srr = await self._llm.call_json(
            prompt=user_prompt,
            system_prompt=system_prompt,
            schema_hint=schema_hint,
            temperature=0.5,
            agent_name="ReqAgent",
        )
        logger.info("[ReqAgent] LLM 返回修改后 SRR: keys={}, requirement.title={}",
                    list(srr.keys()) if isinstance(srr, dict) else type(srr).__name__,
                    srr.get("requirement", {}).get("title", "") if isinstance(srr, dict) else "")

        # 防御：LLM 偶尔返回 list
        if isinstance(srr, list) and srr and isinstance(srr[0], dict):
            logger.warning("[ReqAgent] LLM 返回 list（len={}），取首个 dict 元素", len(srr))
            srr = srr[0]

        # 防御：LLM 偶尔返回非 dict
        if not isinstance(srr, dict):
            logger.error("[ReqAgent] LLM 返回非 dict 结构: type={}", type(srr).__name__)
            raise RuntimeError(
                f"LLM 返回格式异常（期望 dict，实际 {type(srr).__name__}），"
                "请检查 LLM 配置或重试"
            )

        # 3. 后处理：复用 _post_process
        # raw_requirement 传入 SRR JSON 文本，供追溯
        srr_text_for_trace = json.dumps(current_srr, ensure_ascii=False)
        srr = self._post_process(srr, product_id, product_name, srr_text_for_trace)

        req_id = srr["requirement"].get("id", "")
        flows = srr.get("flows", {})
        hp = flows.get("happy_path", {}) if isinstance(flows, dict) else {}
        hp_steps = hp.get("steps", []) if isinstance(hp, dict) else []
        logger.info("[ReqAgent] 后处理完成(refine): req_id={}, priority={}, flows.steps={}",
                    req_id, srr["requirement"].get("priority", ""), len(hp_steps))

        # 4. Schema 宽松校验
        self._validate_lenient(srr)

        # 5. 写入向量库
        try:
            await asyncio.to_thread(
                vector_store.add_requirement,
                req_id=req_id,
                srr_json=srr,
                metadata={
                    "product_id": srr["requirement"].get("product_id", ""),
                    "modules": ",".join(srr["requirement"].get("modules", [])),
                    "priority": srr["requirement"].get("priority", "P2"),
                    "status": "draft",
                },
            )
            logger.debug("[ReqAgent] 向量库写入成功(refine): req_id={}", req_id)
        except Exception as e:
            logger.warning("[ReqAgent] 向量库写入失败(refine，不阻断）: {}", str(e)[:200])

        logger.info("[ReqAgent] ◀ SRR 修改完成 ▶: req_id={}, title={}",
                    req_id, srr["requirement"].get("title", ""))

        # 6. FP 提取：基于修改后的新 SRR（不从原始文件提取，也不从旧 SRR 提取）
        self.last_fps = []
        self.last_fp_dependencies = []
        try:
            # 把修改后的新 SRR JSON 作为 raw_text 传入，让 LLM 从中提取 FP
            new_srr_text = json.dumps(srr, ensure_ascii=False)
            self.last_fps = await self._fp_extract_from_raw(
                raw_text=new_srr_text,
                srr=srr,
                product_name=product_name,
            )
            logger.info("[ReqAgent] FP 提取完成(refine): {} 个功能点", len(self.last_fps))
        except Exception as e:
            logger.warning("[ReqAgent] FP 提取失败(refine，不阻断主流程）: {}", str(e)[:200])

        return srr

    async def _parse_impl(
        self,
        raw_requirement: str,
        product_id: str = "",
        product_name: str = "",
    ) -> dict:
        """解析实现（内部）。"""
        # 1. 构建 Prompt
        user_prompt = prompts.build_user_prompt(raw_requirement, product_id, product_name)
        system_prompt = prompt_manager.get("req_agent", "SYSTEM_PROMPT") + "\n\n" + prompt_manager.get("req_agent", "FEW_SHOT_EXAMPLE")
        logger.debug("[ReqAgent] Prompt 构建完成: user_prompt_len={}, system_prompt_len={}",
                     len(user_prompt), len(system_prompt))

        # 2. 调用 LLM（强制 JSON 输出）
        # v2.1: temperature 从 0.3 提升至 0.5，鼓励 LLM 输出更完整内容
        # max_tokens=None: 不设上限，由 LLM 自身的上下文窗口决定输出长度
        # （当前模型普遍支持 250k~1M token，设 8k 反而会截断输出）
        schema_hint = {
            "srr_version": str,
            "requirement": dict,
            "flows": dict,
            "data_constraints": list,
            "acceptance_criteria": list,  # v2.1 新增字段
            "api_definitions": list,      # v2.1 新增字段
            "additional_details": str,    # v2.1 新增兜底字段
        }
        logger.info("[ReqAgent] 调用 LLM: temperature=0.5, max_tokens=unlimited, schema_keys={}", list(schema_hint.keys()))
        srr = await self._llm.call_json(
            prompt=user_prompt,
            system_prompt=system_prompt,
            schema_hint=schema_hint,
            temperature=0.5,
            agent_name="ReqAgent",
            # v2.1: 不再设 max_tokens 上限，由模型自身上下文窗口决定
        )
        logger.info("[ReqAgent] LLM 返回 SRR: keys={}, requirement.title={}",
                    list(srr.keys()) if isinstance(srr, dict) else type(srr).__name__,
                    srr.get("requirement", {}).get("title", "") if isinstance(srr, dict) else "")

        # 防御：LLM 偶尔返回 list（如 [{...}]），取首个 dict 元素
        if isinstance(srr, list) and srr and isinstance(srr[0], dict):
            logger.warning("[ReqAgent] LLM 返回 list（len={}），取首个 dict 元素", len(srr))
            srr = srr[0]

        # 防御：LLM 偶尔返回非 dict（如字符串），报错
        if not isinstance(srr, dict):
            logger.error("[ReqAgent] LLM 返回非 dict 结构: type={}", type(srr).__name__)
            raise RuntimeError(
                f"LLM 返回格式异常（期望 dict，实际 {type(srr).__name__}），"
                "请检查 LLM 配置或重试"
            )

        # 3. 后处理：补全缺失字段
        srr = self._post_process(srr, product_id, product_name, raw_requirement)
        req_id = srr["requirement"].get("id", "")
        # 防御式日志：LLM 可能返回非预期类型（flows 是 dict 但 happy_path 可能是 list）
        flows = srr.get("flows", {})
        hp = flows.get("happy_path", {}) if isinstance(flows, dict) else {}
        hp_steps = hp.get("steps", []) if isinstance(hp, dict) else []
        alt = flows.get("alternate_flows", []) if isinstance(flows, dict) else []
        alt_flows = alt if isinstance(alt, list) else []
        nf = srr.get("non_functional", {})
        logger.debug("[ReqAgent] 后处理完成: req_id={}, priority={}, modules={}, "
                     "preconditions={}, flows.steps={}, alternate_flows={}, "
                     "cross_fp_flows={}, shared_rules={}, "
                     "non_functional={}, relationships={}, test_points_hint={}, "
                     "legacy_data_impact={}, additional_details_len={}",
                     req_id, srr["requirement"].get("priority", ""),
                     srr["requirement"].get("modules", []),
                     len(srr.get("preconditions", [])),
                     len(hp_steps),
                     len(alt_flows),
                     len(srr.get("cross_fp_interactions", {}).get("cross_fp_flows", [])),
                     len(srr.get("cross_fp_interactions", {}).get("shared_business_rules", [])),
                     len(nf if isinstance(nf, dict) else {}),
                     len(srr.get("relationships", [])),
                     len(srr.get("test_points_hint", [])),
                     "yes" if srr.get("legacy_data_impact") else "no",
                     len(srr.get("additional_details", "")))

        # 4. Schema 宽松校验（v2.1：容错模式，不因非关键字段阻断）
        self._validate_lenient(srr)

        # 5. 写入向量库（供 ClarifyAgent 检索同类需求，标注 draft 状态）
        try:
            await asyncio.to_thread(
                vector_store.add_requirement,
                req_id=req_id,
                srr_json=srr,
                metadata={
                    "product_id": srr["requirement"].get("product_id", ""),
                    "modules": ",".join(srr["requirement"].get("modules", [])),
                    "priority": srr["requirement"].get("priority", "P2"),
                    "status": "draft",  # 分析阶段写入，归档时会被覆盖
                },
            )
            logger.debug("[ReqAgent] 向量库写入成功: req_id={}", req_id)
        except Exception as e:
            # 向量库写入失败不阻断主流程（可能 ONNX 模型未就绪）
            logger.warning("[ReqAgent] 向量库写入失败（不阻断）: {}", str(e)[:200])

        logger.info("[ReqAgent] ◀ 解析完成 ▶ 下一节点 ClarifyAgent: req_id={}, title={}, "
                    "priority={}, modules={}, flows.steps={}, data_constraints={}",
                    req_id, srr["requirement"].get("title", ""),
                    srr["requirement"].get("priority", ""),
                    srr["requirement"].get("modules", []),
                    len(hp_steps),
                    len(srr.get("data_constraints", [])))

        # 6. v1.5.1: 一体式 FP 提取（从完整原始文档拆分功能点 + 子 SRR）
        self.last_fps = []
        self.last_fp_dependencies = []  # P0-3: 同步重置
        try:
            self.last_fps = await self._fp_extract_from_raw(
                raw_text=raw_requirement,
                srr=srr,
                product_name=product_name,
            )
            logger.info("[ReqAgent] FP 提取完成: {} 个功能点", len(self.last_fps))
        except Exception as e:
            logger.warning("[ReqAgent] FP 提取失败（不阻断主流程）: {}", str(e)[:200])

        return srr

    def _post_process(
        self,
        srr: dict,
        product_id: str = "",
        product_name: str = "",
        raw_requirement: str = "",  # v2.1 新增：保存原始文本供追溯
    ) -> dict:
        """后处理：补全缺失的元数据字段。

        Args:
            srr: LLM 解析出的 SRR 字典
            product_id: 调用方传入的产品 ID（优先于 LLM 输出）
            product_name: 调用方传入的产品名称
            raw_requirement: 原始需求文本（保存到 SRR 供追溯）

        Returns:
            dict: 补全后的 SRR 字典

        Note:
            - srr_version 缺失则补 "1.0"
            - requirement.id 缺失则自动生成
            - product_id 优先用调用方传入的值
            - priority 缺失则补 "P2"（默认）
            - modules 缺失则补空列表
            - additional_details 缺失原文则自动兜底（v2.1）
        """
        srr.setdefault("srr_version", "1.0")

        req = srr.get("requirement", {})
        # 防御：LLM 可能将 requirement 输出为列表而非对象
        if not isinstance(req, dict):
            logger.warning("[ReqAgent] SRR.requirement 非 dict: type={}, 重置为空dict", type(req).__name__)
            req = {}
            srr["requirement"] = req
        # 需求 ID：始终生成新的唯一 ID（不信任 LLM 返回的 ID，因为 LLM 可能
        # 从文档中复制硬编码的 ID 如 "REQ-2024-001"，导致重复键冲突）
        llm_id = req.get("id", "")
        if llm_id:
            req["reference_id"] = llm_id  # 保留 LLM 返回的 ID 作为参考
        req["id"] = gen_requirement_id()
        # 产品 ID：调用方传入优先
        if product_id:
            req["product_id"] = product_id
        req.setdefault("product_id", "PROD-DEFAULT")
        if product_name:
            req["product_name"] = product_name
        req.setdefault("priority", "P2")
        req.setdefault("modules", [])
        req.setdefault("change_type", "new")

        # flows 缺失则补空结构
        flows = srr.get("flows", {})
        if not isinstance(flows, dict):
            logger.warning("[ReqAgent] SRR.flows 非 dict: type={}, 重置", type(flows).__name__)
            flows = {}
            srr["flows"] = flows
        # v2.18: 兼容 LLM 返回 happy_path 为数组的情况（应为 {"steps": [...]}）
        hp = flows.get("happy_path")
        if isinstance(hp, list):
            logger.warning("[ReqAgent] SRR.flows.happy_path 为数组（应为对象），自动包装为 {steps: [...]}")
            flows["happy_path"] = {"steps": hp}
        else:
            flows.setdefault("happy_path", {"steps": []})
        flows.setdefault("alternate_flows", [])

        # 其他字段补默认值
        srr.setdefault("preconditions", [])
        # v2.34: 总 SRR 不再保留 business_rules 顶层字段（与 shared_business_rules 重复）
        # 若 LLM 仍输出，由下方迁移逻辑合并到 cross_fp_interactions.shared_business_rules
        srr.setdefault("non_functional", {})
        srr.setdefault("relationships", [])
        srr.setdefault("test_points_hint", [])

        # v2.22: 功能点概要（LLM 首次解析时可能输出，后端 FP 拆分后会基于实际 FP 覆盖）
        srr.setdefault("fp_summary", [])

        # v2.1 字段补默认值
        srr.setdefault("api_definitions", [])
        srr.setdefault("user_roles", [])
        srr.setdefault("dependencies", [])
        srr.setdefault("ui_rules", [])
        srr.setdefault("data_model", [])
        srr.setdefault("assumptions", [])

        # v2.32 新增字段：跨 FP 交互规则（总 SRR 核心职责）
        if not isinstance(srr.get("cross_fp_interactions"), dict):
            srr["cross_fp_interactions"] = {
                "cross_fp_flows": [],
                "shared_business_rules": [],
                "fp_contracts": [],
                "global_constraints": [],
            }
        else:
            cfi = srr["cross_fp_interactions"]
            cfi.setdefault("cross_fp_flows", [])
            cfi.setdefault("shared_business_rules", [])
            cfi.setdefault("fp_contracts", [])
            cfi.setdefault("global_constraints", [])

        # v2.32 新增字段：历史数据影响分析（仅优化需求时由 LLM 输出，否则为 None）
        if "legacy_data_impact" not in srr:
            srr["legacy_data_impact"] = None

        # v2.32/v2.34: 兼容旧 LLM 输出——若 LLM 仍输出了 boundary_conditions /
        # data_constraints / acceptance_criteria / business_rules，迁移到对应位置
        # - boundary_conditions / data_constraints / acceptance_criteria → additional_details
        # - business_rules → cross_fp_interactions.shared_business_rules（去重）
        _legacy_bc = srr.pop("boundary_conditions", None) or []
        _legacy_dc = srr.pop("data_constraints", None) or []
        _legacy_ac = srr.pop("acceptance_criteria", None) or []
        _legacy_br = srr.pop("business_rules", None) or []
        if _legacy_bc or _legacy_dc or _legacy_ac or _legacy_br:
            logger.info(
                "[ReqAgent] v2.34 兼容处理：LLM 仍输出已移除的顶层字段 "
                "(boundary_conditions={}, data_constraints={}, acceptance_criteria={}, business_rules={})，"
                "business_rules 合并到 cross_fp_interactions.shared_business_rules，"
                "其余合并到 additional_details",
                len(_legacy_bc) if isinstance(_legacy_bc, list) else 0,
                len(_legacy_dc) if isinstance(_legacy_dc, list) else 0,
                len(_legacy_ac) if isinstance(_legacy_ac, list) else 0,
                len(_legacy_br) if isinstance(_legacy_br, list) else 0,
            )
            # 1. business_rules 迁移到 shared_business_rules
            if isinstance(_legacy_br, list) and _legacy_br:
                cfi = srr.get("cross_fp_interactions") or {}
                sbr = cfi.setdefault("shared_business_rules", [])
                existing_rules = {
                    r.get("rule", "") if isinstance(r, dict) else str(r)
                    for r in sbr if r is not None
                }
                added = 0
                for item in _legacy_br:
                    if isinstance(item, dict):
                        rule_text = item.get("rule", "")
                        if rule_text and rule_text not in existing_rules:
                            # 补 id（若无）
                            if not item.get("id"):
                                item["id"] = f"BR-MIG-{added + 1}"
                            # 补 applied_fps（若 LLM 未指定，留空待人工补全）
                            if not item.get("applied_fps"):
                                item["applied_fps"] = []
                            sbr.append(item)
                            existing_rules.add(rule_text)
                            added += 1
                    elif isinstance(item, str) and item and item not in existing_rules:
                        sbr.append({
                            "id": f"BR-MIG-{added + 1}",
                            "rule": item,
                            "applied_fps": [],
                        })
                        existing_rules.add(item)
                        added += 1
                if added > 0:
                    logger.info(
                        "[ReqAgent] v2.34 business_rules → shared_business_rules 迁移完成: 新增 {} 条（去重后）",
                        added,
                    )
            # 2. boundary/data_constraints/acceptance_criteria 迁移到 additional_details
            extras: list[str] = []
            if isinstance(_legacy_bc, list):
                extras.extend(f"[边界] {item}" for item in _legacy_bc if isinstance(item, (str, dict)))
            if isinstance(_legacy_dc, list):
                extras.extend(f"[数据约束] {item}" for item in _legacy_dc if isinstance(item, (str, dict)))
            if isinstance(_legacy_ac, list):
                extras.extend(f"[验收] {item}" for item in _legacy_ac if isinstance(item, (str, dict)))
            if extras:
                existing_add = srr.get("additional_details", "") or ""
                srr["additional_details"] = (existing_add + "\n\n" + "\n".join(extras)).strip()

        # v2.1：保存原始需求文本（供追溯和审查完整性）
        if raw_requirement:
            srr.setdefault("_raw_text", raw_requirement)

        # v2.1：additional_details 兜底（LLM 未输出时自动填入原文摘要）
        srr.setdefault("additional_details", "")
        if not srr.get("additional_details") and raw_requirement:
            # 如果 LLM 没有输出 additional_details，截取原文前 500 字作为兜底
            srr["additional_details"] = raw_requirement[:500]

        # v2.1：字段类型归一化（防止 LLM 输出 Schema 之外的 Literal 值）
        self._normalize_literal_fields(srr)

        # v2.1：全面字段名归一化（LLM 常输出不同字段名）
        self._normalize_field_names(srr)

        # v2.9：增量文档兜底识别——若 LLM 未输出 _is_incremental，但原文含增量信号，
        # 则补标记并生成默认基线缺口，确保下游 ClarifyAgent 强制触发 KB 基础补全。
        # （LLM 已输出 _is_incremental 时尊重其判断，不覆盖）
        # v2.14 修复：移除硬编码登录相关 _baseline_gaps（污染非登录需求，如架构文档），
        # 改为留空列表，交由 ClarifyAgent 基于原文动态推导真实缺口。
        if not srr.get("_is_incremental") and raw_requirement:
            if self._looks_like_incremental(raw_requirement):
                srr["_is_incremental"] = True
                # 不注入硬编码缺口，留给 ClarifyAgent 基于原文动态推导
                srr.setdefault("_baseline_gaps", [])
                logger.info("[ReqAgent] 检测到增量文档信号，标记 _is_incremental=true（基线缺口由 ClarifyAgent 推导）")

        return srr

    @staticmethod
    def _looks_like_incremental(text: str) -> bool:
        """启发式判断原始需求文本是否自我声明为增量/优化文档。

        仅作为 LLM 未输出 _is_incremental 时的兜底，避免漏标导致增量文档
        被当成完整独立需求、无法触发知识库基线补全。

        v2.14 修复：移除"标题含版本号视为增量信号"的过宽匹配——
        架构文档普遍含 v2.0 等版本号但并非增量文档，误触发率过高。
        仅保留明确的增量语义信号。
        """
        import re
        if not text:
            return False
        signals = [
            r"相对于\s*V?\d", r"新增与优化", r"完整上下文请参见", r"本文档仅描述",
            r"优化版本", r"在\s*V\d.*基础上", r"基于\s*V\d", r"升级到\s*V\d",
            r"相对于.*版本",
        ]
        for pat in signals:
            if re.search(pat, text):
                return True
        # v2.14：移除标题含版本号的过宽匹配（v\d+\.\d+ 误触发架构文档）
        return False

    # 字段名别名映射表
    # 注意：只在嵌套列表项（boundary_conditions/data_constraints/flows 等）中使用，
    # **不**用于顶层 requirement 对象，避免误改 requirement.summary/title 等核心字段。
    # key 顺序很重要：长别名优先匹配（避免 "rule_id" 被 "rule" 抢先匹配）
    FIELD_NAME_MAP: dict[str, str] = {
        # DataConstraint 字段别名
        "field_name": "field", "column_name": "field", "col": "field", "column": "field",
        # 注：不再把通用 key "name" 映射为 "field"。AlternateFlow.name / ExternalDependency.name
        # 等子对象以 name 作为合法标识符，全局映射会误伤并导致下游生成丢失名称信息（见 Bug 1）。
        "data_type": "type", "field_type": "type", "dtype": "type",
        "nullable": "required", "mandatory": "required",
        "validation": "constraints", "format": "constraints",
        "sample": "example", "default": "example",
        # Step / AlternateFlow 字段别名
        "expected": "expect", "expected_result": "expect", "result": "expect", "failure": "expect",
        "trigger_condition": "trigger", "condition": "trigger",
        # ApiDefinition 相关
        "endpoint": "path", "url": "path", "route": "path",
        "http_method": "method", "request": "request_body", "request_schema": "request_body",
        "response": "response_body", "response_schema": "response_body",
        "errors": "error_responses",
        # AcceptanceCriteria 相关
        "criteria": "description", "desc": "description",
        # UiRule 相关
        "ui_element": "element", "component": "element",
        # ExternalDependency 相关
        "service": "name", "system": "name", "dependency": "name",
        "impact": "impact_if_unavailable", "effect": "impact_if_unavailable",
        # DataModel 相关
        "entity": "entity_name", "table": "entity_name",
        "relations": "relationships",
        # Assumption 相关
        "note": "content", "assumption": "content",
        # BusinessRule 相关
        "rule_id": "id", "rule_name": "rule",
        "rule_text": "rule", "rule_desc": "rule",
    }

    def _normalize_field_names(self, srr: dict) -> None:
        """全局字段名归一化：将 LLM 常输出的别名映射到 Schema 正式字段名。
        
        LLM 在输出 JSON 时经常使用不同的字段名（如 `name` 而非 `field`，
        `expected` 而非 `expect`），导致 Pydantic 校验失败。
        此方法递归扫描所有嵌套结构，进行字段名映射。
        """
        # 每个列表中的 dict 项，对 key 做映射
        list_paths = [
            "preconditions", "boundary_conditions", "business_rules",
            "data_constraints", "relationships", "test_points_hint",
            "acceptance_criteria", "api_definitions", "user_roles",
            "dependencies", "ui_rules", "data_model", "assumptions",
        ]
        for path in list_paths:
            items = srr.get(path, [])
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        self._remap_dict_keys(item)

        # flows 中的嵌套结构
        flows = srr.get("flows", {})
        for flow_type in ("happy_path", "alternate_flows"):
            flow = flows.get(flow_type, {})
            if isinstance(flow, dict):
                steps = flow.get("steps", [])
                if isinstance(steps, list):
                    for step in steps:
                        if isinstance(step, dict):
                            self._remap_dict_keys(step)
            elif isinstance(flow, list):
                for item in flow:
                    if isinstance(item, dict):
                        self._remap_dict_keys(item)
                        # alternate_flows 内部可能还有 steps
                        sub_steps = item.get("steps", [])
                        if isinstance(sub_steps, list):
                            for s in sub_steps:
                                if isinstance(s, dict):
                                    self._remap_dict_keys(s)

        # data_model 中 fields 嵌套
        for dm in srr.get("data_model", []):
            if isinstance(dm, dict):
                self._remap_dict_keys(dm)
                fields = dm.get("fields", [])
                if isinstance(fields, list):
                    for f in fields:
                        if isinstance(f, dict):
                            self._remap_dict_keys(f)

    def _remap_dict_keys(self, d: dict) -> None:
        """将 dict 中 LLM 输出的别名 key 映射为 Schema 正式 key。
        
        规则：仅当 formal key 不存在时才映射，避免覆盖 LLM 已正确输出的字段。
        """
        for alias, formal in self.FIELD_NAME_MAP.items():
            if alias in d and formal not in d:
                d[formal] = d.pop(alias)

    def _validate_lenient(self, srr: dict) -> None:
        """容错模式 Schema 校验。

        不直接使用 SRR.model_validate（严格模式），而是：
        1. 对 Critical 字段做严格检查（title / summary / happy_path）
        2. 用 SRR.model_validate 整体校验（所有子模型已加 extra="ignore"）
        3. 校验失败只记 warning 不阻断，确保 LLM 解析结果能正常进入后续流程
        """
        from pydantic import ValidationError

        # ===== 1. Critical 字段严格检查（阻断式） =====
        critical_errors: list[str] = []
        req = srr.get("requirement", {})
        if not req.get("title"):
            critical_errors.append("requirement.title 缺失")
        if not req.get("summary"):
            critical_errors.append("requirement.summary 缺失")

        # flows.happy_path.steps 缺失不阻断——ClarifyAgent 会检测并提问
        flows = srr.get("flows", {})
        hp = flows.get("happy_path", {}) if isinstance(flows, dict) else {}
        steps = hp.get("steps", []) if isinstance(hp, dict) else []
        if not steps or not isinstance(steps, list) or len(steps) == 0:
            logger.warning("[ReqAgent] flows.happy_path.steps 为空，将由 ClarifyAgent 补充")

        if critical_errors:
            raise RuntimeError(
                f"需求缺少关键字段，无法解析: {'; '.join(critical_errors)}"
            )

        # ===== 2. 整体容错校验（extra="ignore" 已处理多余字段） =====
        try:
            SRR.model_validate(srr)
            logger.info("[ReqAgent] Schema 校验通过 ✅")
        except ValidationError as e:
            err_count = len(e.errors())
            logger.warning(
                "[ReqAgent] Schema 校验有 {} 个非关键问题(已容错，不阻断流程)", err_count
            )
            for err in e.errors():
                loc = ".".join(str(p) for p in err["loc"])
                logger.debug("  - {}: {} (value={})",
                             loc, err["msg"], str(err.get("input", ""))[:60])

    def _normalize_literal_fields(self, srr: dict) -> None:
        """将 LLM 输出的 Literal 字段值归一化为 Schema 允许的值。
        
        当 LLM 输出如 business_rules.type="session" 或 acceptance_criteria.type="custom"
        等 Schema 中未定义的 Literal 值时，映射到最接近的默认值，
        避免 SRR.model_validate 抛出 Literal 校验错误。
        """
        # BusinessRule.type 映射（未识别值 → validation）
        biz_type_map = {
            "session": "security",
            "timeout": "validation",
            "rate_limit": "validation",
            "notification": "workflow",
            "data_retention": "security",
            "encryption": "security",
            "audit": "security",
            "integration": "workflow",
            "authorization": "permission",
            "authentication": "security",
            "cache": "calculation",
            # v1.6.1: 自映射（LLM 直接输出合法类型时不改写）
            "validation": "validation",
            "calculation": "calculation",
            "workflow": "workflow",
            "permission": "permission",
            "security": "security",
            "business": "business",
            "constraint": "constraint",
            "data_integrity": "data_integrity",
            "field_validation": "field_validation",
            "access_control": "access_control",
            "format": "format",
            "mandatory": "mandatory",
            "uniqueness": "uniqueness",
            "prerequisite": "prerequisite",
            "presentation": "presentation",
            "initialization": "initialization",
            "range": "range",
            "enumeration": "enumeration",
            "data_format": "data_format",
            "relationship": "relationship",
            "logic": "logic",
            "duplication": "duplication",
        }
        # v2.34: business_rules 已从总 SRR 移除，但若 LLM 仍输出且尚未迁移，此处做兼容
        # （正常情况下 _post_process 已迁移到 cross_fp_interactions.shared_business_rules）
        for rule in srr.get("business_rules", []):
            if isinstance(rule, dict) and rule.get("type"):
                t = rule["type"].lower().strip()
                if t in biz_type_map:
                    rule["type"] = biz_type_map[t]

        # AcceptanceCriterion.type 规范化（合法值见 srr.py AcceptanceCriterion.type Literal）
        #   functional / performance / security / usability / compatibility / reliability
        # 保留合法值；非规范别名（availability/maintainability/portability）归一；
        # 空值或无法识别的值默认 functional（保证下游 Pydantic Literal 校验通过）。
        # 注意：performance / security 为合法值，必须保留，不得误重置为 functional（见 Bug 2）。
        AC_TYPE_VALID = {
            "functional", "performance", "security",
            "usability", "compatibility", "reliability",
        }
        AC_TYPE_ALIAS = {
            "availability": "reliability",
            "maintainability": "compatibility",
            "portability": "compatibility",
        }
        for ac in srr.get("acceptance_criteria", []):
            if isinstance(ac, dict):
                t = (ac.get("type") or "").lower().strip()
                if t in AC_TYPE_VALID:
                    ac["type"] = t
                elif t in AC_TYPE_ALIAS:
                    ac["type"] = AC_TYPE_ALIAS[t]
                else:
                    ac["type"] = "functional"

    async def _fp_extract_from_raw(
        self,
        raw_text: str,
        srr: dict,
        product_name: str = "",
    ) -> list[dict]:
        """v1.6: 从原始文档一体式提取功能点 + 交互层 SRR。

        流程：
          1. 查询同模块已有 FP → 已存在的名称不再让 LLM 重新生成
          2. LLM 一次性输出 interaction_srr + functional_points（仅新 FP）
          3. 已有 FP + LLM 新生成 FP = 完整 FP 列表

        返回仅含**新生成**的 FP；已有 FP 由调用方从 DB 复制。

        Returns:
            list[dict]: 仅 LLM 新生成的功能点（不含复用已有 FP）
        """
        from app.core import prompt_manager as pm
        import json as _json

        self.interaction_srr = None  # 重置

        req_meta = srr.get("requirement", {})
        modules = req_meta.get("modules", []) or []
        modules_str = ", ".join(modules)

        # ── v1.6: 查询同模块已有 FP，跳过 LLM 生成 ──
        existing_names: list[str] = []
        extra_constraint = ""
        try:
            from app.db.database import async_session
            from app.db.models import FunctionalPoint
            from sqlalchemy import select as sa_select
            async with async_session() as s:
                mod_fps = await s.execute(
                    sa_select(FunctionalPoint.name).where(
                        FunctionalPoint.module_id.in_(modules)
                    ).distinct()
                )
                existing_names = [r[0] for r in mod_fps.fetchall() if r[0]]
        except Exception:
            pass

        if existing_names:
            existing_fp_hint = (
                "\n\n## 以下功能点已存在数据库中——请勿重新生成，只需保留名称和类型"
                "（在 functional_points 中标记 \"reuse_existing\": true，sub_srr 留空）：\n"
                + "\n".join(f"- {n}" for n in existing_names[:30])
            )
            extra_constraint = (
                "\n\n**硬性约束**：以上已存在的功能点名称，禁止在 functional_points 中重新生成内部 sub_srr。"
                "只需输出 name/fp_type/summary 并标记 \"reuse_existing\": true，sub_srr 必须为空对象 {{}}。"
            )
        else:
            existing_fp_hint = ""

        # 构建概要 SRR
        overview_srr = _json.dumps({
            "title": req_meta.get("title", ""),
            "summary": req_meta.get("summary", ""),
            "priority": req_meta.get("priority", "P2"),
            "change_type": req_meta.get("change_type", "new"),
            "preconditions": srr.get("preconditions", [])[:5],
        }, ensure_ascii=False)

        prompt = pm.get("fp_agent", "FP_INTEGRATED_USER_PROMPT").format(
            req_title=req_meta.get("title", "未知需求"),
            modules=modules_str,
            product_name=product_name,
            overview_srr=overview_srr,
            raw_text=raw_text[:96000],
            existing_fp_hint=existing_fp_hint,
            extra_constraint=extra_constraint,
        )

        try:
            result = await self._llm.call_json(
                prompt=prompt,
                system_prompt=pm.get("fp_agent", "FP_INTEGRATED_SYSTEM_PROMPT"),
                schema_hint={"interaction_srr": dict, "functional_points": list, "dependencies": list},
                temperature=0.2,
                agent_name="FpAgent",
            )
            if isinstance(result, dict):
                self.interaction_srr = result.get("interaction_srr")
                self.last_fp_dependencies = result.get("dependencies", [])
                # 过滤掉 LLM 标记为 reuse_existing 的（已在 DB 中，只保留真正新生成的）
                all_fps = result.get("functional_points", [])
                new_fps = [f for f in all_fps if not f.get("reuse_existing")]
                return new_fps
            return []
        except Exception:
            logger.warning("[ReqAgent] FP 一体式提取 LLM 调用失败")
            self.last_fp_dependencies = []
            self.interaction_srr = None
            return []

    @staticmethod
    def update_fp_summary_from_fps(srr: dict, fps: list[dict]) -> dict:
        """v2.22: 基于实际功能点列表更新总 SRR 的 fp_summary 字段。

        在 FP 拆分/合并/重新生成后调用，确保总 SRR 的 fp_summary 与实际 FP 列表一致。
        覆盖 LLM 首次解析时输出的 fp_summary（以实际入库的 FP 为准）。

        Args:
            srr: 总 SRR 字典（会被原地修改）
            fps: 实际功能点列表，每个 FP 至少含 name/fp_type/priority/summary/module_id

        Returns:
            dict: 更新后的 SRR（原 srr 引用）
        """
        fp_summary = []
        for fp in fps:
            if not isinstance(fp, dict):
                continue
            fp_summary.append({
                "name": fp.get("name", ""),
                "fp_type": fp.get("fp_type", "workflow"),
                "priority": fp.get("priority", "P2"),
                "summary": fp.get("summary", ""),
                "module": fp.get("module_id") or fp.get("suggested_module") or "",
            })
        srr["fp_summary"] = fp_summary
        logger.debug("[ReqAgent] fp_summary 已更新: {} 个功能点", len(fp_summary))
        return srr


# 全局单例
req_agent = ReqAgent()

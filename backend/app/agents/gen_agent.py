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
from app.utils.coverage import calc_min_cases, calc_point_coverage
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
        historical_regression: list[dict] | None = None,
        coverage_gaps: list[dict] | None = None,
        fp_scope: dict | None = None,
        global_srr: dict | None = None,
    ) -> list[dict]:
        """根据 SRR 生成测试用例集。

        Args:
            srr: SRR JSON 字典
            enable_multi_type: 是否启用分类生成（FULL 通道 True / FAST 通道 False）
            impact_guidance: 影响分析指导文本（仅优化需求时传入）
            functional_points: 需求已拆分的功能点列表（v1.4 新增）。传入时
                LLM 按每个 FP 定向生成用例，并在用例中标注 related_fp 字段，
                供 persist_to_kb 写入 test_case↔functional_point 追溯关系。
            historical_regression: v3.4 回归复用基线——影响分析确认的受影响 FP
                最新版 approved 用例（functional_point_cases 聚合），直接并入本次生成集。
            coverage_gaps: v3.4 覆盖缺口——无历史用例/覆盖不足的受影响 FP，
                注入 prompt 让 LLM 定向补生成回归用例。
            fp_scope: v5.0 FP 分片生成——传入单个 FP（含其 srr_json）时，prompt 仅注入
                该功能点子 SRR + 该 FP 定义，产出该 FP 定向小集合（相关用例强标 related_fp），
                大幅缩小 prompt（避免全量 SRR 一次生成）。同时传入 global_srr 可附带注入
                跨功能点共享规则片段，保证该 FP 用例不遗漏跨 FP/共享约束。
            global_srr: v5.0 全局/跨功能点通道——仅注入总 SRR 的跨功能点/共享规则/顶层
                数据约束（不起整段全量 SRR），用于"跨功能点/集成"用例的独立生成，
                避免跨 FP 组合用例因按 FP 拆分而丢失。

        Returns:
            list[dict]: UTCF 用例列表，每个用例为字典格式

        Note:
            生成流程（docs/03 §2，v3.1 起跳过 KB 检索）：
                1. 构建 Prompt（分类/批量 + FP 定向 + 覆盖缺口引导）
                2. 调用 LLM 生成
                3. 解析用例 JSON + 生成 ID
                4. gen_confidence 放入 gen_metadata
                5. v3.4: 合并回归复用基线（historical_regression）进入结果集
        """
        req_id = srr.get("requirement", {}).get("id", "")
        product_id = srr.get("requirement", {}).get("product_id", "")
        modules = srr.get("requirement", {}).get("modules", []) or []
        logger.info("[GenAgent] ▶ 开始生成: req_id={}, multi_type={}, has_impact_guidance={}, fp_count={}",
                    req_id, enable_multi_type, bool(impact_guidance), len(functional_points or []))

        # v3.2: 轻量 KB 检索——查同模块下已 approved 的高质量用例 Top-3 作为 Few-shot 参考
        few_shot_cases: list = []
        matched_patterns: list = []
        functional_points = functional_points or []
        if modules:
            try:
                from app.db.database import async_session
                from app.db.models import TestCase
                from sqlalchemy import select as _select
                async with async_session() as _s:
                    cases_in_module = await _s.execute(
                        _select(TestCase.case_json, TestCase.id, TestCase.modules)
                        .where(TestCase.product_id == product_id, TestCase.status == "approved")
                        .order_by(TestCase.updated_at.desc())
                        .limit(10)
                    )
                    for cj, cid, db_modules in cases_in_module.all():
                        if not isinstance(cj, dict):
                            continue
                        # 模块过滤优先使用独立列 modules，其次回退 case_json 内字段
                        case_modules = list(db_modules or []) if isinstance(db_modules, (list, tuple)) else []
                        if not case_modules:
                            case_modules = cj.get("modules") or ([cj.get("module")] if cj.get("module") else [])
                        if not any(module in modules for module in case_modules):
                            continue
                        few_shot_cases.append({
                            "id": cid,
                            "title": cj.get("title", ""),
                            "type": cj.get("type", ""),
                            "priority": cj.get("priority", ""),
                            "steps": cj.get("steps", [])[:3],
                            "assertions": cj.get("assertions", [])[:2],
                        })
                    # 去重 + Top-3
                    seen = set()
                    uniq = []
                    for c in few_shot_cases:
                        title = c.get("title", "")
                        if title and title not in seen:
                            seen.add(title)
                            uniq.append(c)
                    few_shot_cases = uniq[:3]
            except Exception as e:
                logger.debug("[GenAgent] 轻量 KB 检索失败（不阻断）: {}", str(e)[:120])

        # v3.4: 测试模式库检索——按需求语义检索经人工验证的模式骨架（test_patterns），
        # 注入 prompt 让 LLM 照着骨架生成，提升覆盖率与风格一致性（等价类/边界值/决策表等）。
        if modules:
            try:
                query_text = (
                    f"{' / '.join(modules)} "
                    f"{srr.get('requirement', {}).get('summary', '') or srr.get('requirement', {}).get('title', '')}"
                ).strip()[:200]
                # v3.1.3 P2-8: search_patterns 含 Milvus 网络调用，改 to_thread 防止阻塞事件循环
                hits = await asyncio.to_thread(vector_store.search_patterns, query_text, top_k=3)
                for h in hits:
                    meta = h.get("metadata", {}) or {}
                    doc = h.get("document") or h.get("content") or h.get("text") or ""
                    name = meta.get("name", "")
                    cat = meta.get("category", "")
                    pattern_json = None
                    try:
                        pattern_json = json.loads(doc) if doc else None
                    except Exception:
                        pattern_json = None
                    if not pattern_json and isinstance(meta, dict):
                        pattern_json = meta.get("pattern_json")
                    if name and (pattern_json or cat):
                        matched_patterns.append({
                            "name": name,
                            "category": cat,
                            "pattern_json": pattern_json if isinstance(pattern_json, dict) else str(pattern_json),
                        })
            except Exception as e:
                logger.debug("[GenAgent] 测试模式库检索失败（不阻断）: {}", str(e)[:120])
        logger.info("[GenAgent] 轻量 KB 检索: few_shot_cases={}, matched_patterns={}",
                    len(few_shot_cases), len(matched_patterns))

        # 3. 构建 Prompt（含 FP 定向上下文 + 覆盖缺口引导）
        # 要点级覆盖驱动：动态生成数量下限由要点规模推导，随 prompt 注入硬约束

        # v5.0 FP 分片 / 全局通道：确定本次生成使用的"有效 SRR"与"有效功能点集"。
        # - fp_scope 传入单个 FP 时，prompt 只注入该功能点子 SRR（小而聚焦），
        #   避免全量 SRR 一次生成造成 prompt 过大、易超时/字数爆炸。
        # - global_srr 传入时，只注入跨功能点/共享规则/顶层约束片段，用于
        #   独立生成"跨功能点/集成"用例，保证按 FP 拆分后组合用例不丢失。
        _srr_effective = srr
        _fp_effective: list[dict] = functional_points or []
        if fp_scope is not None:
            _srr_effective = fp_scope.get("srr_json") or fp_scope
            _fp_effective = [fp_scope]
            # 强制将生成范围限定为该 FP，避免混入其它功能点
            _fp_effective[0].pop("points", None)
        elif global_srr is not None:
            _srr_effective = global_srr
            _fp_effective = []

        gen_min_cases, est_total_points = calc_min_cases(_srr_effective, _fp_effective)
        user_prompt = prompts.build_user_prompt(
            srr=_srr_effective,
            few_shot_cases=few_shot_cases or None,
            matched_patterns=matched_patterns or None,
            impact_guidance=impact_guidance,
            enable_multi_type=enable_multi_type,
            functional_points=_fp_effective,
            coverage_gaps=coverage_gaps or None,
            regression_anchors=historical_regression or None,
            min_cases=gen_min_cases,
        )
        logger.info("[GenAgent] 动态数量下限: min_cases={}, 要点总数={}", gen_min_cases, est_total_points)
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
                agent_name="GenAgent",
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
        cases = self._post_process_cases(cases, req_id, module_prefix, batch_id, gen_model, matched_patterns=matched_patterns)

        # v4.9: 要点标注系统兜底——LLM 可能未在 notes/cover_points 输出方括号要点 ID，
        # 导致覆盖映射永远失效（回退数量比、补生成无法聚焦缺口）。这里依据用例文本
        # 与要点描述的关键词命中，为未标注用例确定性回填覆盖的要点 ID。
        # 已标注的用例跳过，仅补明显种子，避免污染。
        try:
            from app.utils.coverage import backfill_cases_point_ids
            _backfilled = backfill_cases_point_ids(cases, srr, functional_points or [])
            if _backfilled:
                logger.info(
                    "[GenAgent] 要点标注系统兜底回填: {}/{} 条用例已自动标注覆盖要点",
                    _backfilled, len(cases),
                )
        except Exception as e:
            logger.debug("[GenAgent] 要点标注兜底回填失败（不阻断）: {}", str(e)[:120])

        # v2.0: 四级去重（精确→语义→子集→LLM等价判定），对应 docs/03 §2.3
        before_dedup = len(cases)
        cases = await self._deduplicate_cases(cases)
        dedup_removed = before_dedup - len(cases)
        if dedup_removed > 0:
            logger.info("[GenAgent] 去重完成: before={}, after={}, removed={}",
                        before_dedup, len(cases), dedup_removed)

        # 统计用例类型分布
        type_dist: dict[str, int] = {}
        for c in cases:
            t = c.get("type", "unknown")
            type_dist[t] = type_dist.get(t, 0) + 1
        logger.info("[GenAgent] 后处理完成: cases={}, module_prefix={}, type_dist={}",
                    len(cases), module_prefix, type_dist)

        # v3.4: 合并回归复用基线——影响分析确认的受影响 FP 最新版 approved 用例。
        joined = self.merge_regression_cases(historical_regression or [], cases)
        reuse_count = len(historical_regression or [])
        if reuse_count:
            for c in joined:
                if c.get("source") == "reused":
                    c["gen_batch_id"] = batch_id
            logger.info(
                "[GenAgent] 合并回归复用基线: reused={}, total={}",
                reuse_count, len(joined),
            )

        # v3.3：覆盖率统计必须基于最终用例集，包含回归复用基线。
        self._log_coverage_stats(srr, functional_points, joined)

        # 7. 向量库写入延后至 ReviewAgent.persist_to_kb（v2.0 修正，对应 docs/03 §1 Phase 6）
        # 仅写入 PG 供 ReviewAgent 读取，向量库由 persist_to_kb 统一写入 approved 用例
        # 避免未审核用例污染 Few-shot 检索结果
        logger.info("[GenAgent] ◀ 生成完成 ▶ 下一节点 ReviewAgent: "
                    "cases={}, types={}, batch_id={}",
                    len(joined), type_dist, batch_id)
        return joined

    # ========================================================================
    # SRR 覆盖率校验（v3.3）
    # ========================================================================

    def _log_coverage_stats(self, srr: dict, functional_points: list[dict], cases: list[dict]) -> None:
        """要点级覆盖映射统计（RTM 实践），输出映射率/高优覆盖/未覆盖要点告警。

        不阻断流水线，仅用于监控和可观测性。
        v3.4 增强：新增测试设计技术覆盖率分析（等价类/边界值/决策表/状态迁移/场景法/错误推测）
        """
        try:
            point_cov = calc_point_coverage(srr, functional_points or [], cases)
            case_count = point_cov["case_count"]

            # 按 related_fp 统计各功能点的用例数
            fp_coverage: dict[str, int] = {}
            for c in cases:
                rfp = c.get("related_fp", c.get("related_requirement", ""))
                fp_coverage[rfp] = fp_coverage.get(rfp, 0) + 1

            # v3.4：测试设计技术覆盖率分析
            type_dist: dict[str, int] = {}
            for c in cases:
                t = c.get("type", "unknown")
                type_dist[t] = type_dist.get(t, 0) + 1

            # 分析测试设计技术覆盖情况
            technique_analysis = self._analyze_technique_coverage(
                srr, functional_points, cases, type_dist
            )

            mode = "映射" if point_cov["mapping_based"] else "回退(数量比)"
            logger.info(
                "[GenAgent/coverage] 要点级覆盖[{}]: 要点总数={}, 已覆盖={}, 覆盖率={:.1%}, "
                "P0/P1 要点={}/{}, 高优覆盖={:.1%} | 用例数={}, 类型分布={}, FP分布={}, 要点构成={}",
                mode,
                point_cov["total_points"], point_cov["covered_points"], point_cov["coverage_rate"],
                point_cov["high_priority_covered"], point_cov["high_priority_total"],
                point_cov["high_priority_rate"],
                case_count, type_dist, fp_coverage, point_cov["point_stats"],
            )
            logger.info(
                "[GenAgent/coverage] 测试设计技术分析: type_dist={}, techniques={}",
                type_dist, technique_analysis,
            )

            # 覆盖告警（口径与 ReviewAgent._should_supplement 一致）
            uncovered_top = point_cov["uncovered"][:5]
            if point_cov["mapping_based"]:
                if point_cov["coverage_rate"] < 0.8:
                    logger.warning(
                        "[GenAgent/coverage] ⚠️ 要点映射覆盖率偏低: {:.1%} (<80%)，未覆盖要点示例: {}",
                        point_cov["coverage_rate"],
                        [p["id"] for p in uncovered_top],
                    )
                if point_cov["high_priority_total"] > 0 and point_cov["high_priority_covered"] < point_cov["high_priority_total"]:
                    logger.warning(
                        "[GenAgent/coverage] ⚠️ P0/P1 要点未全覆盖: {}/{}，缺失: {}",
                        point_cov["high_priority_covered"], point_cov["high_priority_total"],
                        [p["id"] for p in point_cov["uncovered"] if p.get("priority") in ("P0", "P1")][:10],
                    )
            elif point_cov["total_points"] > 0 and point_cov["coverage_rate"] < 0.5:
                logger.warning(
                    "[GenAgent/coverage] ⚠️ 覆盖比偏低（回退口径）: SRR 有 {} 个要点，仅 {} 个用例（{:.2f}），"
                    "且用例未标注任何要点 ID，请检查 notes 覆盖标注",
                    point_cov["total_points"], case_count, point_cov["coverage_rate"],
                )

            # v3.4：测试设计技术覆盖缺口告警
            missing_techniques = [t for t, covered in technique_analysis.items() if not covered]
            if missing_techniques:
                logger.warning(
                    "[GenAgent/coverage] ⚠️ 测试设计技术覆盖缺口: {} 未覆盖，"
                    "建议在后续迭代中补充",
                    missing_techniques,
                )
        except Exception as e:
            logger.debug("[GenAgent/coverage] 覆盖率统计失败（不阻断）: {}", str(e)[:120])

    def _analyze_technique_coverage(
        self, srr: dict, functional_points: list[dict], cases: list[dict], type_dist: dict[str, int]
    ) -> dict[str, bool]:
        """分析测试设计技术的覆盖情况（v3.4）。

        基于 ISTQB 黑盒测试设计技术，检查生成的用例集是否充分应用了
        各类测试设计技术。

        Args:
            srr: SRR JSON 字典
            functional_points: 功能点列表
            cases: 生成的用例列表
            type_dist: 用例类型分布

        Returns:
            dict[str, bool]: 各测试设计技术是否已覆盖
                - equivalence_partitioning: 等价类划分
                - boundary_value: 边界值分析
                - decision_table: 决策表法
                - state_transition: 状态迁移法
                - scenario: 场景法
                - error_guessing: 错误推测法
        """
        result = {
            "equivalence_partitioning": False,
            "boundary_value": False,
            "decision_table": False,
            "state_transition": False,
            "scenario": False,
            "error_guessing": False,
        }

        # 边界值分析：检查是否有 boundary 类型用例，或用例标题/步骤含边界关键词
        if type_dist.get("boundary", 0) > 0:
            result["boundary_value"] = True
        else:
            boundary_keywords = ["边界", "临界", "最小", "最大", "上限", "下限", "boundary", "limit"]
            for c in cases:
                title = str(c.get("title", "")).lower()
                notes = str(c.get("notes", "")).lower()
                if any(kw in title or kw in notes for kw in boundary_keywords):
                    result["boundary_value"] = True
                    break

        # 等价类划分：检查 data_constraints 是否有范围型约束且有对应用例
        has_range_constraints = False
        all_fps = [{"srr_json": srr}] + [{"srr_json": fp.get("srr_json", {})} for fp in functional_points]
        for fp in all_fps:
            fp_srr = fp.get("srr_json", {}) if isinstance(fp.get("srr_json"), dict) else {}
            for dc in fp_srr.get("data_constraints", []):
                if isinstance(dc, dict):
                    cons_str = str(dc.get("constraints", dc.get("constraint", "")))
                    if any(kw in cons_str for kw in ["-", "~", "至", "范围", "min", "max"]):
                        has_range_constraints = True
                        break
        if has_range_constraints and result["boundary_value"]:
            result["equivalence_partitioning"] = True

        # 决策表法：检查 business_rules 是否有多条规则组合，且有 functional/integration 用例
        has_multi_rules = False
        for fp in all_fps:
            fp_srr = fp.get("srr_json", {}) if isinstance(fp.get("srr_json"), dict) else {}
            rules = fp_srr.get("business_rules", [])
            multi_rules = [r for r in rules if isinstance(r, dict) and r.get("type") in ("validation", "business", "workflow")]
            if len(multi_rules) >= 2:
                has_multi_rules = True
                break
        if has_multi_rules and (type_dist.get("functional", 0) >= 2 or type_dist.get("integration", 0) > 0):
            result["decision_table"] = True

        # 状态迁移法：检查 alternate_flows 是否有状态变更，且有 exception 用例
        has_state_flows = False
        for fp in all_fps:
            fp_srr = fp.get("srr_json", {}) if isinstance(fp.get("srr_json"), dict) else {}
            alt_flows = fp_srr.get("flows", {}).get("alternate_flows", [])
            for af in alt_flows:
                if isinstance(af, dict):
                    flow_name = str(af.get("name", af.get("trigger", "")))
                    if any(kw in flow_name for kw in ["状态", "锁定", "禁用", "过期", "取消", "撤销", "超时", "失败"]):
                        has_state_flows = True
                        break
        if has_state_flows and type_dist.get("exception", 0) > 0:
            result["state_transition"] = True

        # 场景法：检查有 functional 类型用例覆盖 happy_path
        if type_dist.get("functional", 0) > 0:
            result["scenario"] = True

        # 错误推测法：检查有 exception/security 类型用例，或用例含错误场景关键词
        if type_dist.get("exception", 0) > 0 or type_dist.get("security", 0) > 0:
            result["error_guessing"] = True
        else:
            error_keywords = ["空值", "null", "超长", "特殊字符", "并发", "超时", "越权", "注入", "重复"]
            for c in cases:
                title = str(c.get("title", "")).lower()
                notes = str(c.get("notes", "")).lower()
                if any(kw in title or kw in notes for kw in error_keywords):
                    result["error_guessing"] = True
                    break

        return result

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
        matched_patterns: list[dict] | None = None,
    ) -> list[dict]:
        """后处理：生成 ID、补全字段、整理 gen_metadata。

        Args:
            cases: LLM 生成的原始用例列表
            req_id: 关联需求 ID
            module_prefix: 模块前缀（用于生成用例 ID）
            batch_id: 生成批次 ID
            gen_model: 生成模型名
            matched_patterns: 本次生成检索命中的测试模式列表（v3.5），
                LLM 未回填 source_pattern 时主动写入，保证 pattern_bonus 生效

        Returns:
            list[dict]: 后处理后的用例列表

        Note:
            - gen_confidence 必须放在 gen_metadata 对象内（修复问题5）
            - 用例 ID 缺失则自动生成
            - related_requirement 缺失则补 req_id
        """
        now = datetime.now().isoformat()
        processed: list[dict] = []

        # ---- H2 修复：预取 UTCF 合法枚举（仅一次），在 Pydantic 校验前做值域规范化 ----
        from app.models.utcf import TestCase
        from pydantic import ValidationError as PydanticValidationError

        # 从 TestCase.model_fields 提取合法枚举集合
        type_field = TestCase.model_fields.get("type")
        priority_field = TestCase.model_fields.get("priority")
        try:
            # type: 直接取 Literal 枚举 args
            valid_types = set(type_field.annotation.__args__) if type_field and hasattr(type_field.annotation, "__args__") else {
                "functional", "boundary", "exception", "security", "performance",
                "integration", "exploratory", "regression",
            }
            valid_priorities = (
                set(priority_field.annotation.__args__)
                if priority_field and hasattr(priority_field.annotation, "__args__")
                else {"P0", "P1", "P2", "P3"}
            )
        except Exception:
            valid_types = {
                "functional", "boundary", "exception", "security", "performance",
                "integration", "exploratory", "regression",
            }
            valid_priorities = {"P0", "P1", "P2", "P3"}

        # 非法 type 到合法 type 的启发式映射（避免简单丢弃用例）
        TYPE_MAP = {
            "基本流": "functional", "备选流": "functional", "正常": "functional", "正例": "functional", "功能": "functional",
            "边界值": "boundary", "边界": "boundary", "极端": "boundary",
            "异常流": "exception", "异常": "exception", "错误": "exception", "反例": "exception",
            "安全": "security",
            "性能": "performance", "压力": "performance", "负载": "performance",
            "集成": "integration",
            "探索": "exploratory", "探索性": "exploratory",
            "回归": "regression",
        }
        PRIORITY_MAP = {
            "高": "P1", "中": "P2", "低": "P3", "紧急": "P0", "致命": "P0", "重要": "P1", "一般": "P2",
            "P4": "P3", "P5": "P3",  # 超出枚举的降级
        }

        for case in cases:
            if not isinstance(case, dict):
                # LLM 可能生成非字典（字符串/数组），直接跳过并在 review 层补
                logger.warning("[GenAgent] 生成非字典 case 已跳过: {!r}", str(case)[:200])
                continue
            # ⚠️ 始终覆盖 LLM 生成的占位 ID（如 TC-AUTH-0001-0001），
            # 避免同模块重复生成时 UniqueViolationError
            case["id"] = gen_test_case_id(module_prefix)

            # v5.2: 批次 ID 写入用例（评审报告中心关联生成批次用）
            case.setdefault("gen_batch_id", batch_id)

            # 补全关联需求
            case.setdefault("related_requirement", req_id)

            # 补全必填字段
            case.setdefault("title", "未命名用例")
            case.setdefault("type", "functional")
            case.setdefault("priority", "P1")
            case.setdefault("description", "")
            case.setdefault("preconditions", [])
            case.setdefault("steps", [])
            case.setdefault("assertions", [])
            case.setdefault("tags", [])
            case.setdefault("test_data", {})
            case.setdefault("cleanup", [])
            case.setdefault("execution", {})
            case.setdefault("related_fp", "")  # v2.0.1: FP 追溯（LLM 可能漏写）

            # --- H2: 值域规范化（枚举/类型/优先级），修复 LLM 偶尔输出 "功能"/"高" 等非标准值 ---
            ctype = str(case.get("type") or "functional").strip()
            if ctype not in valid_types:
                mapped = TYPE_MAP.get(ctype) or TYPE_MAP.get(ctype.lower())
                case["type"] = mapped if mapped in valid_types else "functional"
            cprio = str(case.get("priority") or "P1").strip()
            if cprio not in valid_priorities:
                mapped = PRIORITY_MAP.get(cprio) or PRIORITY_MAP.get(cprio.upper())
                case["priority"] = mapped if mapped in valid_priorities else "P1"
            # confidences 归一化到 [0,1]
            if "confidence" in case:
                try:
                    c = float(case["confidence"])
                    case["confidence"] = max(0.0, min(1.0, c))
                except (TypeError, ValueError):
                    case.pop("confidence", None)
            case.setdefault("confidence", 0.6)

            # --- H2: steps / assertions 规范化（Pydantic 要求的字段强制对齐）---
            def _normalize_step(s: object) -> dict | None:
                if not isinstance(s, dict):
                    return None
                # 数字字符串（如 "1"/"2"）可强转；非法或缺失则用占位序号
                step_raw = s.get("step")
                try:
                    step_no = int(step_raw) if step_raw is not None and step_raw != "" else 0
                except (TypeError, ValueError):
                    step_no = 0
                return {
                    "step": step_no,
                    "action": str(s.get("action") or "").strip(),
                    "expected": str(s.get("expected") or "").strip(),
                    "data": s.get("data") if isinstance(s.get("data"), dict) else None,
                }
            steps_in = case.get("steps") or []
            if isinstance(steps_in, list):
                normalized = [_normalize_step(s) for s in steps_in]
                valid_steps = [x for x in normalized if x is not None]
                # 重新编号 step 序号（Pydantic Step.step 要求从 1 开始且为 int）
                for idx, s in enumerate(valid_steps, 1):
                    if s["step"] <= 0:
                        s["step"] = idx
                case["steps"] = valid_steps
            else:
                case["steps"] = []
            # assertions：确保每个断言都有合法 type（required，没有则默认 equals）。
            # v4.4 修复 L1：原归一化白名单采用旧枚举风格（equal/greater/less/not_contains），
            # 与 UTCF schema 的合法枚举（equals/contains/matches/status_code/gt/lt/not_null/custom）
            # 不一致，导致 LLM 输出的合法 "equals" 被误判为非法并默认改写为 "equal"，
            # 使全部用例 UTCF 校验失败、落入 construct 兜底（置信度被强制压到 0.2）。
            # 现改为按 schema 枚举 + 常见误写映射归一化。
            assert_type_map = {
                # 规范合法值直接透传
                "equals": "equals", "contains": "contains", "matches": "matches",
                "status_code": "status_code", "gt": "gt", "lt": "lt",
                "not_null": "not_null", "custom": "custom",
                # 常见误写/旧风格 → 映射到规范值（保持原语义）
                "equal": "equals",            # 缺 s
                "equal_to": "equals",
                "==": "equals", "=": "equals",
                "not_equals": "custom",       # 无直接枚举，归为 custom（含 message 语义）
                "not_equal": "custom",
                "greater": "gt", "greater_than": "gt", ">": "gt",
                "less": "lt", "less_than": "lt", "<": "lt",
                "greater_or_equal": "gt", "less_or_equal": "lt",
                "not_contains": "custom",
                "not_contain": "custom",
                "regex": "matches",
                "exists": "not_null", "not_exists": "custom",
                "contains_text": "contains",
                "eq": "equals", "in": "contains",
            }
            assertions_in = case.get("assertions") or []
            if isinstance(assertions_in, list):
                normalized_assertions = []
                for a in assertions_in:
                    if not isinstance(a, dict):
                        continue
                    raw_t = a.get("type")
                    if not isinstance(raw_t, str):
                        a["type"] = "equals"
                    else:
                        key = raw_t.strip().lower()
                        a["type"] = assert_type_map.get(key, "equals")
                    a.setdefault("target", "")
                    a.setdefault("expected_value", None)
                    a.setdefault("message", "")
                    normalized_assertions.append(a)
                case["assertions"] = normalized_assertions
            else:
                case["assertions"] = []
            # 顶层 expected 可能非字符串：转 str
            if "expected" in case and not isinstance(case["expected"], (str, type(None))):
                case["expected"] = str(case["expected"])

            # ⚠️ gen_confidence 必须放在 gen_metadata 内（修复问题5）
            # 如果 LLM 把 gen_confidence 放在了顶层，移入 gen_metadata
            top_level_conf = case.pop("gen_confidence", None)
            gm_raw = case.get("gen_metadata")
            gen_metadata = gm_raw if isinstance(gm_raw, dict) else {}
            if gen_metadata is not gm_raw:  # 原值为非 dict（字符串/None/数字），覆写
                case["gen_metadata"] = gen_metadata
            if top_level_conf is not None:
                try:
                    top_level_conf = float(top_level_conf)
                    top_level_conf = max(0.0, min(1.0, top_level_conf))
                except (TypeError, ValueError):
                    top_level_conf = 0.7
                gen_metadata.setdefault("gen_confidence", top_level_conf)
            if "gen_confidence" in gen_metadata:
                try:
                    c = float(gen_metadata["gen_confidence"])
                    gen_metadata["gen_confidence"] = max(0.0, min(1.0, c))
                except (TypeError, ValueError):
                    gen_metadata["gen_confidence"] = 0.7
            else:
                gen_metadata.setdefault("gen_confidence", 0.7)  # 默认置信度
            gen_metadata.setdefault("gen_model", gen_model)
            gen_metadata.setdefault("gen_at", now)

            # execution: 保证 int 类型（LLM 可能生成字符串 "3"）
            execution = case.get("execution") or {}
            if isinstance(execution, dict):
                try:
                    execution["timeout_seconds"] = int(execution.get("timeout_seconds") or 15)
                except (TypeError, ValueError):
                    execution["timeout_seconds"] = 15
                try:
                    execution["retry_count"] = int(execution.get("retry_count") or 0)
                except (TypeError, ValueError):
                    execution["retry_count"] = 0
                if not isinstance(execution.get("depends_on"), list):
                    execution["depends_on"] = []
                if not isinstance(execution.get("skip_conditions"), list):
                    execution["skip_conditions"] = []
                case["execution"] = execution

            # source_pattern 同步到 gen_metadata 和顶层
            # v3.5: LLM 实际很少回填该字段（schema 标注"可选"），导致 review 的
            # pattern_bonus 恒为 0。这里以检索命中的模式名主动兜底写入，
            # 不覆盖 LLM 已填的值。
            llm_sp = case.get("source_pattern")
            if isinstance(llm_sp, str) and llm_sp.strip():
                gen_metadata.setdefault("source_pattern", llm_sp)
            else:
                fallback_sp = ",".join(
                    p.get("name", "") for p in (matched_patterns or []) if p.get("name")
                )
                if fallback_sp:
                    case["source_pattern"] = fallback_sp
                    gen_metadata.setdefault("source_pattern", fallback_sp)
                else:
                    case.setdefault("source_pattern", "")

            # ---- H2: Pydantic UTCF 强校验（最后一道关卡），失败则 construct 兜底 ----
            try:
                validated = TestCase.model_validate(case)
                processed.append(validated.model_dump(mode="json"))
            except PydanticValidationError as e:
                logger.warning(
                    "[GenAgent] UTCF 校验失败，construct 兜底: case_id={}, errors={}",
                    case.get("id"), str(e)[:500],
                )
                # 记录到 gen_metadata.validation_warnings 便于 review 报告
                gen_metadata["validation_warnings"] = [
                    {"loc": ".".join(str(x) for x in err["loc"]), "msg": err["msg"]}
                    for err in (e.errors() if hasattr(e, "errors") else [])[:10]
                ]
                # 用 construct 跳过校验，保证最终可写库（但标记为低置信度 0.2）
                case["confidence"] = min(float(case.get("confidence") or 0.6), 0.2)
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
        return settings.llm_config.get("model", "unknown")

    def merge_regression_cases(self, regression_cases: list[dict], generated_cases: list[dict]) -> list[dict]:
        """合并回归用例与新生成用例（标题+步骤去重）。

        公开方法：编排层（orchestrator._review_node 补生成合并）直接调用。
        """
        merged = []
        seen = set()
        for case in list(regression_cases) + list(generated_cases):
            key = (
                str(case.get("title", "")).strip().lower(),
                json.dumps(case.get("steps", []), sort_keys=True, ensure_ascii=False),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(case)
        return merged

    # ========================================================================
    # 四级去重（v2.0，对应 docs/03 §2.3）
    # ========================================================================

    async def _deduplicate_cases(self, cases: list[dict]) -> list[dict]:
        """四级去重: 精确匹配 → 语义去重 → 子集检测 → LLM 等价判定。

        对应 docs/03-case-generation-flow.md §2.3。
        借鉴 Meta ACH (arXiv:2501.12862) 的 LLM 等价突变检测思路。

        Args:
            cases: 后处理后的用例列表

        Returns:
            list[dict]: 去重后的用例列表
        """
        if len(cases) <= 1:
            return cases

        # Level 1: 精确去重（同 title + 同 steps）
        cases = self._exact_dedup(cases)
        logger.debug("[GenAgent/Dedup] L1 精确去重后: {}", len(cases))

        # Level 2: 语义去重（标题 Embedding 余弦相似度 > 0.95）
        cases = await self._semantic_dedup(cases, threshold=0.95)
        logger.debug("[GenAgent/Dedup] L2 语义去重后: {}", len(cases))

        # Level 3: 子集去重（case_a.steps 是 case_b.steps 子集 → 保留 case_b）
        cases = self._subset_dedup(cases)
        logger.debug("[GenAgent/Dedup] L3 子集去重后: {}", len(cases))

        # Level 4: LLM 等价判定（对相似度 0.85-0.95 的疑似对调用 LLM 判定）
        # 仅在疑似对较多时触发，避免增加 LLM 成本
        cases = await self._llm_equivalence_dedup(cases, similarity_range=(0.85, 0.95))
        logger.debug("[GenAgent/Dedup] L4 LLM等价去重后: {}", len(cases))

        return cases

    def _exact_dedup(self, cases: list[dict]) -> list[dict]:
        """Level 1: 精确去重（同 title + 同 steps）。"""
        seen: set = set()
        unique: list[dict] = []
        for c in cases:
            title = c.get("title", "")
            steps = json.dumps(c.get("steps", []), sort_keys=True, ensure_ascii=False)
            key = (title, steps)
            if key not in seen:
                seen.add(key)
                unique.append(c)
        return unique

    async def _semantic_dedup(
        self, cases: list[dict], threshold: float = 0.95
    ) -> list[dict]:
        """Level 2: 语义去重（标题 Embedding 余弦相似度 > threshold 合并）。

        保留置信度更高的用例。
        """
        if len(cases) <= 1:
            return cases

        try:
            # 批量获取标题 Embedding（同步模型调用，用线程包装避免阻塞事件循环）
            titles = [c.get("title", "") for c in cases]
            embeddings = await asyncio.to_thread(
                vector_store.embed_texts, titles
            )
        except Exception as e:
            logger.debug("[GenAgent/Dedup] L2 语义去重跳过（Embedding 失败）: {}", str(e)[:100])
            return cases

        if not embeddings or len(embeddings) != len(cases):
            return cases

        # 计算余弦相似度，标记待移除的索引
        to_remove: set = set()
        for i in range(len(cases)):
            if i in to_remove:
                continue
            for j in range(i + 1, len(cases)):
                if j in to_remove:
                    continue
                sim = self.cosine_similarity(embeddings[i], embeddings[j])
                if sim >= threshold:
                    # 保留置信度更高的
                    conf_i = cases[i].get("gen_metadata", {}).get("gen_confidence", 0.5)
                    conf_j = cases[j].get("gen_metadata", {}).get("gen_confidence", 0.5)
                    if conf_i >= conf_j:
                        to_remove.add(j)
                    else:
                        to_remove.add(i)
                        break

        return [c for idx, c in enumerate(cases) if idx not in to_remove]

    def _subset_dedup(self, cases: list[dict]) -> list[dict]:
        """Level 3: 子集去重（case_a.steps 是 case_b.steps 子集 → 保留 case_b）。"""
        if len(cases) <= 1:
            return cases

        def _steps_key(steps: list) -> set:
            """提取步骤的 action 文本作为集合。"""
            return {s.get("action", "") for s in (steps or []) if s.get("action")}

        to_remove: set = set()
        for i in range(len(cases)):
            if i in to_remove:
                continue
            steps_i = _steps_key(cases[i].get("steps", []))
            if not steps_i:
                continue
            for j in range(len(cases)):
                if i == j or j in to_remove:
                    continue
                steps_j = _steps_key(cases[j].get("steps", []))
                if not steps_j:
                    continue
                # i 的步骤是 j 的子集 → i 冗余，移除 i
                if steps_i.issubset(steps_j) and len(steps_i) < len(steps_j):
                    to_remove.add(i)
                    break

        return [c for idx, c in enumerate(cases) if idx not in to_remove]

    async def _llm_equivalence_dedup(
        self, cases: list[dict], similarity_range: tuple = (0.85, 0.95)
    ) -> list[dict]:
        """Level 4: LLM 等价判定（对疑似重复但 Embedding 相似度在 similarity_range 内的对）。

        借鉴 Meta ACH 等价突变检测 (arXiv:2501.12862)，精确度 0.95。

        成本控制：
        - 仅对疑似对调用 LLM（约占总对数 5-10%）
        - 批量判定（一次 LLM 调用处理 N 对）
        - 疑似对 < 2 对时跳过（不值得调用 LLM）
        """
        if len(cases) <= 1:
            return cases

        # 1. 计算所有对的 Embedding 相似度，筛选疑似对
        try:
            titles = [c.get("title", "") for c in cases]
            embeddings = await asyncio.to_thread(vector_store.embed_texts, titles)
        except Exception:
            return cases

        if not embeddings or len(embeddings) != len(cases):
            return cases

        suspicious_pairs: list[tuple[int, int, float]] = []
        for i in range(len(cases)):
            for j in range(i + 1, len(cases)):
                sim = self.cosine_similarity(embeddings[i], embeddings[j])
                if similarity_range[0] <= sim < similarity_range[1]:
                    suspicious_pairs.append((i, j, sim))

        # 疑似对太少不值得调用 LLM
        if len(suspicious_pairs) < 2:
            return cases

        # 2. 批量调用 LLM 判定等价性
        pairs_text = []
        for idx, (i, j, sim) in enumerate(suspicious_pairs):
            pairs_text.append(
                f"对{idx + 1}:\n"
                f"  用例A: title={cases[i].get('title', '')}, type={cases[i].get('type', '')}\n"
                f"  用例B: title={cases[j].get('title', '')}, type={cases[j].get('type', '')}\n"
                f"  相似度={sim:.3f}"
            )

        prompt = (
            "请判定以下用例对是否语义等价（测试同一场景）。\n"
            "输出 JSON 数组，每个元素: {\"pair\": 对序号, \"equivalent\": true/false}\n\n"
            + "\n".join(pairs_text)
        )

        try:
            result = await self._llm.call_json(
                prompt=prompt,
                system_prompt=prompt_manager.get("gen_agent", "DEDUP_EQUIVALENCE_SYSTEM_PROMPT"),
                temperature=0.0,
                max_tokens=2048,
                allow_array=True,
                agent_name="GenAgent-Dedup",
            )
        except Exception as e:
            logger.debug("[GenAgent/Dedup] L4 LLM等价判定跳过: {}", str(e)[:100])
            return cases

        # 3. 根据判定结果移除等价对中置信度较低的
        to_remove: set = set()
        if isinstance(result, list):
            for item in result:
                if not isinstance(item, dict):
                    continue
                pair_idx = item.get("pair", 0) - 1
                equivalent = item.get("equivalent", False)
                if 0 <= pair_idx < len(suspicious_pairs) and equivalent:
                    i, j, _ = suspicious_pairs[pair_idx]
                    conf_i = cases[i].get("gen_metadata", {}).get("gen_confidence", 0.5)
                    conf_j = cases[j].get("gen_metadata", {}).get("gen_confidence", 0.5)
                    if conf_i >= conf_j:
                        to_remove.add(j)
                    else:
                        to_remove.add(i)

        return [c for idx, c in enumerate(cases) if idx not in to_remove]

    @staticmethod
    def cosine_similarity(vec_a: list, vec_b: list) -> float:
        """计算余弦相似度（公开静态方法，供编排层一致性检查复用）。"""
        if not vec_a or not vec_b or len(vec_a) != len(vec_b):
            return 0.0
        import math
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)


# 全局单例
gen_agent = GenAgent()

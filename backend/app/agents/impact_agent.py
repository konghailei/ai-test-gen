"""ImpactAnalysisAgent 影响分析 Agent（模块依赖图谱 BFS 版）。

混合分析策略（方案 C — 最成熟方案）：
  Step 1（模块级粗筛）：给定项目涉及模块 → 模块依赖图 BFS → 受影响模块
  Step 2（需求级定位）：受影响模块 → 追溯矩阵 → 需回归的用例

设计依据：
- docs/03-case-generation-flow.md §1.5（ImpactAnalysisAgent 设计）
- docs/02-knowledge-base.md §2.4（模块依赖图谱）
- 混合方案：模块级粗筛 + 追溯矩阵精确定位

工作流程：
  ① 检测是否为优化需求 → ② 模块依赖图 BFS 遍历 →
  ③ 用例操作规划 → ④ 回归范围精确定位 → ⑤ 构建生成指导

输入：SRR JSON（含 optimization_context）+ 原始用例列表
输出：影响分析报告（含 impact / case_operations / regression_scope）
"""

from __future__ import annotations

import json
from collections import deque
from typing import Any

from sqlalchemy import or_, select

from app.core.llm_client import llm_client
from app.db.database import async_session
from app.db.models import (
    ModuleDependency, Module, Requirement, TestCase as TestCaseModel,
    FunctionalPoint, FPDependency,
)
from app.prompts import impact_agent as prompts
from app.utils.logger import logger


class ImpactAnalysisAgent:
    """影响分析 Agent（模块依赖图谱 BFS + 追溯矩阵精确定位）。"""

    def __init__(self) -> None:
        self._llm = llm_client

    async def analyze(
        self,
        srr: dict,
        original_cases: list[dict] | None = None,
    ) -> dict:
        """分析变更影响范围。

        Args:
            srr: SRR JSON 字典
            original_cases: 原始用例列表

        Returns:
            dict: 影响分析报告
                {
                    "is_optimization": bool,
                    "impact": {
                        "direct_modules": [...],      # 直接涉及的模块
                        "indirect_modules": [...],     # 图谱 BFS 找到的间接受影响模块
                        "all_affected_modules": [...], # 所有受影响模块
                        "direct_requirements": [...],  # 直接需求
                        "indirect_requirements": [...],# 间接受影响的需求
                    },
                    "case_operations": {update, add, delete, keep},
                    "regression_scope": [...],         # 需回归的用例
                    "generation_guidance": str
                }
        """
        opt_context = srr.get("optimization_context")
        req_id = srr.get("requirement", {}).get("id", "")
        req_data = srr.get("requirement", {})

        # ① 检测是否为优化需求
        if not opt_context:
            logger.info("[ImpactAgent] 非优化需求，跳过: req_id={}", req_id)
            return {"is_optimization": False, "generation_guidance": ""}

        logger.info("[ImpactAgent] ▶ 优化需求影响分析: req_id={}", req_id)
        diff = opt_context.get("diff", {})
        original_cases = original_cases or []

        # ② 模块依赖图 BFS 遍历
        involved_modules = req_data.get("modules", []) or []
        async with async_session() as session:
            impact = await self._analyze_module_impact(involved_modules, session)
            logger.info("[ImpactAgent] 模块图分析: direct={}, indirect={}",
                        len(impact["direct_modules"]), len(impact["indirect_modules"]))

            # ③ 追溯矩阵定位：受影响模块下的所有需求 → 用例
            regression_reqs = await self._find_regression_requirements(
                impact["all_affected_modules"], srr, session
            )

        # ④ 用例操作规划
        case_operations = self._plan_case_operations(diff, original_cases)

        # ⑤ 回归范围
        regression_cases = self._plan_regression(case_operations, original_cases)

        # 构建影响报告
        impact_report = {
            "is_optimization": True,
            "impact": {
                "direct_modules": impact["direct_modules"],
                "indirect_modules": impact["indirect_modules"],
                "all_affected_modules": impact["all_affected_modules"],
                "direct_requirements": [req_id],
                "indirect_requirements": regression_reqs,
            },
            "case_operations": case_operations,
            "regression_scope": regression_cases,
            "generation_guidance": self._build_guidance(impact, regression_reqs, case_operations),
        }

        logger.info("[ImpactAgent] ◀ 完成: dir_mod={}, indir_mod={}, reg_reqs={}, reg_cases={}",
                    len(impact["direct_modules"]), len(impact["indirect_modules"]),
                    len(regression_reqs), len(regression_cases))
        return impact_report

    # ========================================================================
    # 模块依赖图 BFS 遍历（核心算法）
    # ========================================================================

    @staticmethod
    def _strength_cn(strength: str) -> str:
        """将强度转为中文描述。"""
        return {"strong": "强依赖", "medium": "中等依赖", "weak": "弱依赖"}.get(strength, strength)

    async def _analyze_module_impact(
        self, involved_modules: list[str], session
    ) -> dict:
        """基于模块依赖图 BFS 遍历，找到所有受影响模块。

        BFS 规则：
        - 从 involved_modules 出发
        - 沿 DEPENDS_ON 边反向遍历（A depends_on B → B 变更影响 A）
        - 沿 RELATED_TO 边双向遍历
        - 最大遍历深度：3 跳

        Returns:
            dict: {direct_modules, indirect_modules, all_affected_modules}
        """
        direct = list(set(involved_modules))
        all_affected = set(direct)
        indirect = []

        # 按需查询依赖边：仅加载与当前涉及模块相关的边
        # v2.2: 模块 ID 含产品前缀（如 "auth_PROD-001"），天然形成产品范围，无需额外 product_id 过滤
        result = await session.execute(
            select(ModuleDependency).where(
                or_(
                    ModuleDependency.source_module_id.in_(direct),
                    ModuleDependency.target_module_id.in_(direct),
                )
            )
        )
        edges = result.scalars().all()

        # 构建邻接表
        in_edges = {}   # target → [source]  （谁依赖我）
        out_edges = {}  # source → [target] （我依赖谁）
        for e in edges:
            st, tg = e.source_module_id, e.target_module_id
            if e.relation_type in ("depends_on", "related_to"):
                in_edges.setdefault(tg, []).append((st, e.relation_type, e.strength))
                out_edges.setdefault(st, []).append((tg, e.relation_type, e.strength))

        # BFS
        queue = deque((m, 0) for m in direct)
        visited = set(direct)

        while queue:
            current, depth = queue.popleft()
            if depth >= 3:
                continue

            # 反向边：依赖当前模块的其他模块（当前模块变更会影响它们）
            for neighbor, rtype, strength in in_edges.get(current, []):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, depth + 1))
                    if neighbor not in direct:
                        hop_label = "直接依赖" if depth == 0 else f"{depth + 1}级间接影响"
                        indirect.append({
                            "module_id": neighbor,
                            "reason": f"{hop_label} · {self._strength_cn(strength)}",
                            "depth": depth + 1,
                        })

            # 正向边：关联模块（双向影响）
            for neighbor, rtype, strength in out_edges.get(current, []):
                if rtype == "related_to" and neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, depth + 1))
                    if neighbor not in direct:
                        hop_label = "直接关联" if depth == 0 else f"{depth + 1}级间接影响"
                        indirect.append({
                            "module_id": neighbor,
                            "reason": f"{hop_label} · 关联关系",
                            "depth": depth + 1,
                        })

        return {
            "direct_modules": direct,
            "indirect_modules": indirect,
            "all_affected_modules": list(visited),
        }

    # ========================================================================
    # 追溯矩阵精确定位
    # ========================================================================

    async def _find_regression_requirements(
        self, affected_modules: list[str], srr: dict, session
    ) -> list[dict]:
        """在受影响模块中，找到需要回归的需求。

        排除当前需求自身，仅返回受影响模块下的其他需求。
        """
        current_req_id = srr.get("requirement", {}).get("id", "")
        current_product_id = srr.get("requirement", {}).get("product_id", "")

        if not affected_modules:
            return []

        # v2.2: 按 product_id 过滤，防止跨产品数据泄漏
        query_filters = [
            Requirement.id != current_req_id,
            Requirement.module.in_(affected_modules),
        ]
        if current_product_id:
            query_filters.append(Requirement.product_id == current_product_id)

        result = await session.execute(
            select(Requirement).where(*query_filters).order_by(Requirement.created_at.desc())
        )
        candidate_reqs = result.scalars().all()

        regression_reqs = []
        for req in candidate_reqs:
            req_modules = set(req.modules or [])
            if req_modules & set(affected_modules):
                regression_reqs.append({
                    "id": req.id,
                    "title": req.title,
                    "matched_modules": list(req_modules & set(affected_modules)),
                })

        return regression_reqs

    # ========================================================================
    # 用例操作规划
    # ========================================================================

    def _plan_case_operations(self, diff: dict, original_cases: list[dict]) -> dict:
        """规划测试用例操作：update/add/delete/keep。"""
        ops: dict[str, list] = {"update": [], "add": [], "delete": [], "keep": []}
        updated_ids: set[str] = set()
        deleted_ids: set[str] = set()

        for added in diff.get("added", []):
            ops["add"].append({"reason": f"新增: {added}", "guidance": f"需为 {added} 生成测试用例"})

        # 功能点删除/修改时，只与用例标题做子串匹配，减少 steps/test_data 中的误匹配
        for removed in diff.get("removed", []):
            for c in original_cases:
                cid = c.get("id", "")
                if cid in deleted_ids:
                    continue
                title = c.get("title", "")
                if removed in title:
                    ops["delete"].append({"case_id": cid, "reason": f"功能已删除: {removed}"})
                    deleted_ids.add(cid)

        for modified in diff.get("modified", []):
            for c in original_cases:
                cid = c.get("id", "")
                if cid in updated_ids or cid in deleted_ids:
                    continue
                title = c.get("title", "")
                if modified in title:
                    ops["update"].append({"case_id": cid, "reason": f"功能已修改: {modified}"})
                    updated_ids.add(cid)

        changed_ids = updated_ids | deleted_ids
        for c in original_cases:
            cid = c.get("id", "")
            if cid and cid not in changed_ids:
                ops["keep"].append({"case_id": cid, "reason": "功能未变，需回归"})

        return ops

    def _plan_regression(self, case_operations: dict, original_cases: list[dict]) -> list[dict]:
        """规划回归范围。"""
        regression = []
        for op in case_operations.get("keep", []):
            regression.append({"case_id": op.get("case_id", ""), "reason": "功能未变，需回归验证"})
        for op in case_operations.get("update", []):
            regression.append({"case_id": op.get("case_id", ""), "reason": "功能已修改，需回归验证"})
        return regression

    def _build_guidance(self, impact: dict, regression_reqs: list[dict], case_ops: dict) -> str:
        """构建生成指导文本（注入 GenAgent Prompt）。"""
        direct = ", ".join(impact.get("direct_modules", []))
        indirect = ", ".join(m.get("module_id", "") for m in impact.get("indirect_modules", []))
        reg_req_titles = ", ".join(r.get("title", "") for r in regression_reqs[:5])

        return (
            f"【影响分析指导】\n"
            f"直接涉及模块: {direct or '无'}\n"
            f"间接受影响模块: {indirect or '无'}\n"
            f"需回归的需求: {reg_req_titles or '无'}\n"
            f"需新增用例: {len(case_ops.get('add', []))} 个\n"
            f"需更新用例: {len(case_ops.get('update', []))} 个\n"
            f"需删除用例: {len(case_ops.get('delete', []))} 个\n"
            f"需回归用例: {len(case_ops.get('keep', [])) + len(case_ops.get('update', []))} 个"
        )

    # ========================================================================
    # v3.0 FP 影响分析（独立触发，不依赖 optimization_context）
    # ========================================================================

    async def _analyze_fp_impact(self, fp_id: str, session) -> dict:
        """分析功能点变更的影响范围（FP + 模块双图谱）。

        步骤：
            1. 反向 BFS FPDependency（找谁依赖此 FP，≤3 跳）
            2. 取 FP 所属 module_id，调用 _analyze_module_impact() 找受影响模块
            3. 用受影响模块匹配同产品其他需求
            4. LLM 合成 description + test_focus

        Args:
            fp_id: 功能点 ID
            session: 数据库会话

        Returns:
            dict: 含 fp 信息 / affected_fps / affected_modules / affected_requirements
                  / description / test_focus / generated_at / is_auto_generated
        """
        # 1. 查询当前 FP
        fp_record = await session.get(FunctionalPoint, fp_id)
        if not fp_record:
            raise ValueError(f"功能点 {fp_id} 不存在")

        fp_info = {
            "fp_id": fp_record.id,
            "fp_name": fp_record.name,
            "fp_type": fp_record.fp_type,
            "module_id": fp_record.module_id or "",
            "req_id": fp_record.requirement_id,
        }

        # 2. 反向 BFS FPDependency（找谁依赖此 FP）
        affected_fps = await self._bfs_fp_reverse(fp_id, session)

        # 3. 模块级影响分析（复用现有方法）
        affected_modules_raw: list[dict] = []
        affected_requirements: list[dict] = []
        if fp_info["module_id"]:
            module_impact = await self._analyze_module_impact([fp_info["module_id"]], session)
            # 查询模块名称
            mod_names: dict[str, str] = {}
            all_mod_ids = module_impact.get("all_affected_modules", [])
            if all_mod_ids:
                mod_result = await session.execute(
                    select(Module.id, Module.name).where(Module.id.in_(all_mod_ids))
                )
                mod_names = {row[0]: row[1] for row in mod_result.all()}

            affected_modules_raw = [
                {
                    "module_id": m.get("module_id", ""),
                    "module_name": mod_names.get(m.get("module_id", ""), m.get("module_id", "")),
                    "reason": m.get("reason", ""),
                    "depth": m.get("depth", 1),
                }
                for m in module_impact.get("indirect_modules", [])
                if m.get("module_id", "") in mod_names
            ]
            # 直接模块也加入
            for mid in module_impact.get("direct_modules", []):
                if mid in mod_names:
                    affected_modules_raw.append({
                        "module_id": mid,
                        "module_name": mod_names.get(mid, mid),
                        "reason": "直接模块",
                        "depth": 0,
                    })

            # 4. 受影响需求（复用 _find_regression_requirements 逻辑）
            # 需要构造一个伪 SRR 来调用现有方法
            current_req = await session.get(Requirement, fp_record.requirement_id)
            if current_req:
                pseudo_srr = {
                    "requirement": {
                        "id": current_req.id,
                        "product_id": current_req.product_id,
                    }
                }
                affected_requirements = await self._find_regression_requirements(
                    module_impact["all_affected_modules"], pseudo_srr, session
                )

        # 5. LLM 合成 description + test_focus
        description, test_focus = await self._build_fp_impact_description(
            fp_info, affected_fps, affected_modules_raw, affected_requirements
        )

        from datetime import datetime, timezone
        return {
            **fp_info,
            "affected_fps": affected_fps,
            "affected_modules": affected_modules_raw,
            "affected_requirements": affected_requirements,
            "description": description,
            "test_focus": test_focus,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "is_auto_generated": True,
        }

    async def _bfs_fp_reverse(self, fp_id: str, session) -> list[dict]:
        """反向 BFS FPDependency 图，找到谁依赖此 FP。

        规则：
        - 从 fp_id 出发，沿反向边遍历（target=fp_id → source 依赖它）
        - depends_on / data_flows / invariants / lifecycle_scenarios / conflicts_with 均遍历
        - 最大深度 3 跳

        Returns:
            list[dict]: 每项含 {fp_id, fp_name, relation_type, req_id, req_title, depth}
        """
        # 查询所有以 fp_id 为 target 的依赖边
        result = await session.execute(
            select(FPDependency).where(
                or_(
                    FPDependency.target_fp_id == fp_id,
                    FPDependency.source_fp_id == fp_id,
                )
            )
        )
        all_edges = result.scalars().all()

        # 构建反向邻接表：target → [(source, relation_type, description)]
        # 同时构建正向邻接表用于双向关系
        reverse_adj: dict[str, list[tuple[str, str, str]]] = {}
        forward_adj: dict[str, list[tuple[str, str, str]]] = {}
        edge_pairs = set()
        for e in all_edges:
            src, tgt = e.source_fp_id, e.target_fp_id
            edge_pairs.add((src, tgt, e.relation_type))
            # 反向：谁指向我（target=fp_id → source 依赖我）
            reverse_adj.setdefault(tgt, []).append((src, e.relation_type, e.description or ""))
            # 正向：我指向谁
            forward_adj.setdefault(src, []).append((tgt, e.relation_type, e.description or ""))

        # BFS（优先反向，因为我们要找"谁依赖此 FP"）
        visited: set[str] = {fp_id}
        affected: list[dict] = []
        queue: deque = deque([(fp_id, 0)])

        # 收集所有受影响 FP ID（用于名称查询）
        affected_fp_ids: set[str] = set()
        bfs_results: list[tuple[str, str, int, str]] = []  # (fp_id, relation_type, depth, description)

        while queue:
            current, depth = queue.popleft()
            if depth >= 3:
                continue
            # 反向边：谁依赖当前 FP
            for neighbor, rtype, desc in reverse_adj.get(current, []):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, depth + 1))
                    affected_fp_ids.add(neighbor)
                    bfs_results.append((neighbor, rtype, depth + 1, desc))
            # 正向边（仅双向关系）：conflicts_with / invariants / lifecycle_scenarios
            for neighbor, rtype, desc in forward_adj.get(current, []):
                if rtype in ("conflicts_with", "invariants", "lifecycle_scenarios") and neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, depth + 1))
                    affected_fp_ids.add(neighbor)
                    bfs_results.append((neighbor, rtype, depth + 1, desc))

        if not affected_fp_ids:
            return []

        # 批量查询 FP 名称 + 所属需求
        fp_result = await session.execute(
            select(
                FunctionalPoint.id, FunctionalPoint.name,
                FunctionalPoint.requirement_id,
            ).where(FunctionalPoint.id.in_(affected_fp_ids))
        )
        fp_map = {row[0]: {"name": row[1], "req_id": row[2]} for row in fp_result.all()}

        # 查询需求标题
        req_ids = {info["req_id"] for info in fp_map.values() if info["req_id"]}
        req_title_map: dict[str, str] = {}
        if req_ids:
            req_result = await session.execute(
                select(Requirement.id, Requirement.title).where(Requirement.id.in_(req_ids))
            )
            req_title_map = {row[0]: row[1] or "" for row in req_result.all()}

        relation_cn = {
            "depends_on": "依赖",
            "data_flows": "数据流向",
            "invariants": "共享约束",
            "lifecycle_scenarios": "生命周期",
            "conflicts_with": "冲突",
        }

        for fp_id_found, rtype, depth, desc in bfs_results:
            info = fp_map.get(fp_id_found, {})
            req_id = info.get("req_id", "")
            affected.append({
                "fp_id": fp_id_found,
                "fp_name": info.get("name", ""),
                "relation_type": rtype,
                "relation_cn": relation_cn.get(rtype, rtype),
                "req_id": req_id,
                "req_title": req_title_map.get(req_id, ""),
                "depth": depth,
                "description": desc,
            })

        return affected

    async def _build_fp_impact_description(
        self,
        fp: dict,
        affected_fps: list[dict],
        affected_modules: list[dict],
        affected_requirements: list[dict],
    ) -> tuple[str, str]:
        """用 LLM 合成自然语言影响描述 + 测试关注点。

        Args:
            fp: 当前功能点信息
            affected_fps: 受影响 FP 列表
            affected_modules: 受影响模块列表
            affected_requirements: 受影响需求列表

        Returns:
            tuple: (description, test_focus)
        """
        # 无任何影响时，直接返回降级文本（不调用 LLM）
        if not affected_fps and not affected_modules and not affected_requirements:
            return (
                f"功能点「{fp.get('fp_name', '')}」当前无被依赖关系，变更影响范围仅限自身。",
                f"重点验证功能点「{fp.get('fp_name', '')}」自身的 happy_path 和边界条件。",
            )

        try:
            result = await llm_client.call_json(
                prompt=prompts.build_fp_impact_description_prompt(
                    fp, affected_fps, affected_modules, affected_requirements,
                ),
                system_prompt=prompts.FP_IMPACT_DESCRIPTION_SYSTEM_PROMPT,
                schema_hint={"description": str, "test_focus": str},
                temperature=0.3,
            )
            description = result.get("description", "")
            test_focus = result.get("test_focus", "")
            if not description:
                description = f"功能点「{fp.get('fp_name', '')}」变更影响 {len(affected_fps)} 个功能点、{len(affected_modules)} 个模块、{len(affected_requirements)} 个需求。"
            if not test_focus:
                test_focus = "重点回归受影响功能点的核心流程。"
            return description, test_focus
        except Exception as e:
            logger.warning("[ImpactAgent] FP 影响描述 LLM 合成失败，降级: {}", str(e)[:200])
            # 降级：纯规则拼接
            fp_names = ", ".join(f.get("fp_name", "") for f in affected_fps[:5])
            mod_names = ", ".join(m.get("module_name", "") for m in affected_modules[:5])
            req_titles = ", ".join(r.get("title", "") for r in affected_requirements[:5])
            description = (
                f"功能点「{fp.get('fp_name', '')}」变更影响："
                f"功能点[{fp_names or '无'}]; 模块[{mod_names or '无'}]; 需求[{req_titles or '无'}]。"
            )
            test_focus = "重点回归受影响功能点的 happy_path 和关键 alternate_flows。"
            return description, test_focus


# 全局单例
impact_agent = ImpactAnalysisAgent()

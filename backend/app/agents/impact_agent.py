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
        """ImpactAnalysisAgent 主入口：规则化影响分析（BFS 依赖图），输出受影响 FP 与生成指导。
        """
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

        # 加载所有模块依赖边构建完整邻接表
        # v2.8.51 修复：原实现仅加载与 direct 相关的边，导致 BFS 只能遍历 1 跳，
        # 2-3 跳的间接受影响模块被漏掉，回归测试范围被低估。
        # 模块数量通常有限，全表加载性能可接受。
        # v2.2: 模块 ID 含产品前缀（如 "auth_PROD-001"），天然形成产品范围，无需额外 product_id 过滤
        result = await session.execute(select(ModuleDependency))
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

        v2.35: 移除 affected_requirements 分析（不再分析受影响的需求）。

        步骤：
            1. 反向 BFS FPDependency（找谁依赖此 FP，≤3 跳）
            2. 取 FP 所属 module_id，调用 _analyze_module_impact() 找受影响模块
            3. LLM 合成 description + test_focus

        Args:
            fp_id: 功能点 ID
            session: 数据库会话

        Returns:
            dict: 含 fp 信息 / affected_fps / affected_modules
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
        # v2.8.55 Bug B-2: 不仅分析起始 FP 的模块，还要分析所有受影响 FP 的模块
        affected_modules_raw: list[dict] = []
        
        # 收集所有相关模块 ID（起始 FP + 所有受影响 FP）
        all_module_ids: set[str] = set()
        if fp_info["module_id"]:
            all_module_ids.add(fp_info["module_id"])
        # 批量查询受影响 FPs 的 module_id
        if affected_fps:
            affected_fp_ids = [fp["fp_id"] for fp in affected_fps]
            mod_query = await session.execute(
                select(FunctionalPoint.module_id).where(
                    FunctionalPoint.id.in_(affected_fp_ids),
                    FunctionalPoint.module_id.isnot(None),
                )
            )
            for (mod_id,) in mod_query.all():
                if mod_id:
                    all_module_ids.add(mod_id)
        
        if all_module_ids:
            module_impact = await self._analyze_module_impact(list(all_module_ids), session)
            # 查询模块名称
            mod_names: dict[str, str] = {}
            all_mod_ids_from_impact = module_impact.get("all_affected_modules", [])
            if all_mod_ids_from_impact:
                mod_result = await session.execute(
                    select(Module.id, Module.name).where(Module.id.in_(all_mod_ids_from_impact))
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

        # 4. 规则合成 description + test_focus（v3.1.7：不再调 LLM）
        description, test_focus = self._build_fp_impact_description(
            fp_info, affected_fps, affected_modules_raw
        )

        from datetime import datetime, timezone
        return {
            **fp_info,
            "affected_fps": affected_fps,
            "affected_modules": affected_modules_raw,
            "description": description,
            "test_focus": test_focus,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "is_auto_generated": True,
            # 默认未确认（None = 候选，区别于 True=确认受影响 / False=确认不受影响）。
            # v3.1.3 修复 P5：原默认 False 会被展示层误读为「确认不受影响」，
            # 与 run_impact_analysis 写入的未确认候选语义（is_affected=None）不一致。
            # confirm_impact_analysis 确认后会覆盖为 True/False；confirm_requirement
            # 守卫只认 confirmed_at，不受此值影响。
            "is_affected": None,
        }

    async def _bfs_fp_reverse(self, fp_id: str, session) -> list[dict]:
        """双向 BFS FPDependency 图，找到受影响功能点（含"被依赖功能点"）。

        规则：
        - 反向遍历（target=fp_id → source 依赖它）：找"谁依赖此 FP"（下游调用方）
        - 正向遍历（source=fp_id → target）：找"此 FP 依赖谁"（被依赖功能点/上游）
        - 所有 relation_type 均遍历；最大深度 3 跳
        - v2.3: 仅遍历 review_status='approved' 且 is_active=True 的依赖边
          （未审核依赖不参与影响分析；归档后的历史版本依赖也不参与）
        - v3.1: 修复——原实现对单向关系（depends_on/data_flows）不遍历正向边，
          导致"被依赖功能点"（此 FP 依赖的 target）漏出影响范围。现放开正向遍历，
          并在返回项标记 direction 以区分"被依赖"（outgoing）与"依赖者"（incoming）。

        Returns:
            list[dict]: 每项含 {fp_id, fp_name, relation_type, relation_cn,
                                 req_id, req_title, depth, description, direction}
        """
        # v2.45: 仅遍历 review_status='approved' 且 is_active=True 的依赖边
        # （未审核依赖不参与影响分析；项目归档后的历史版本依赖也不参与）
        # v2.8.52: 修复 BFS 只能 1 跳的问题——原实现仅加载与起始 fp_id 直接相连的边，
        # 导致 2-3 跳的间接受影响 FP 全部漏掉，回归测试范围被严重低估。
        # 改为加载所有 approved + active 边构建完整邻接表（与模块 BFS 一致）。
        result = await session.execute(
            select(FPDependency).where(
                FPDependency.review_status == "approved",
                FPDependency.is_active == True,  # noqa: E712
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
        bfs_results: list[tuple[str, str, int, str, str]] = []  # (fp_id, relation_type, depth, description, direction)

        while queue:
            current, depth = queue.popleft()
            if depth >= 3:
                continue
            # 反向边：谁依赖当前 FP（下游调用方，direction=incoming）
            for neighbor, rtype, desc in reverse_adj.get(current, []):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, depth + 1))
                    affected_fp_ids.add(neighbor)
                    bfs_results.append((neighbor, rtype, depth + 1, desc, "incoming"))
            # 正向边：此 FP 依赖谁（被依赖功能点/上游，direction=outgoing）
            # v3.1: 放开——原仅遍历双向关系，导致 depends_on/data_flows 的单向
            # "被依赖功能点"漏出影响范围。现对所有 rtype 沿正向边遍历。
            for neighbor, rtype, desc in forward_adj.get(current, []):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, depth + 1))
                    affected_fp_ids.add(neighbor)
                    bfs_results.append((neighbor, rtype, depth + 1, desc, "outgoing"))

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

        for fp_id_found, rtype, depth, desc, direction in bfs_results:
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
                # v3.1: 方向标记——incoming=谁依赖此FP（下游），outgoing=此FP依赖谁（被依赖功能点）
                "direction": direction,
            })

        return affected

    def _build_fp_impact_description(
        self,
        fp: dict,
        affected_fps: list[dict],
        affected_modules: list[dict],
    ) -> tuple[str, str]:
        """规则合成影响描述 + 测试关注点（v3.1.7 改造：砍掉 LLM 文字润色轮子）。

        v2.x-v3.1.6 曾调用 LLM 把图谱分析结果润色成自然语言——但图谱分析
        （反向 BFS + 模块传播）本身已是确定性代码，LLM 只是复读输入且存在
        编造风险（可能虚构未被图谱识别的依赖）。v3.1.7 起改为纯规则拼接：
        输出确定、无幻觉、省一次 LLM 调用。

        Args:
            fp: 当前功能点信息 {fp_id, fp_name, ...}
            affected_fps: 受影响 FP 列表（反向 BFS 结果，含 depth/reason 可选）
            affected_modules: 受影响模块列表（含 module_name/reason/depth 可选）

        Returns:
            tuple: (description, test_focus)
        """
        fp_name = fp.get("fp_name", "")
        if not affected_fps and not affected_modules:
            return (
                f"功能点「{fp_name}」当前无被依赖关系，变更影响范围仅限自身。",
                f"重点验证功能点「{fp_name}」自身的 happy_path 和边界条件。",
            )

        # 受影响功能点：同名合并，按深度排序展示（近的在前）
        fp_parts = []
        for f_ in affected_fps[:8]:
            name = f_.get("fp_name", "") or f_.get("fp_id", "")
            if name:
                depth = f_.get("depth")
                fp_parts.append(f"{name}（{'直接' if depth == 0 else f'间接{depth}层'}）" if isinstance(depth, int) else name)
        mod_parts = []
        for m in affected_modules[:8]:
            name = m.get("module_name", "") or m.get("module_id", "")
            reason = m.get("reason", "")
            if name:
                mod_parts.append(f"{name}（{reason}）" if reason and reason not in name else name)

        desc_lines = [f"功能点「{fp_name}」变更影响："]
        if fp_parts:
            desc_lines.append(f"- 受影响功能点：{'、'.join(fp_parts)}")
        if mod_parts:
            desc_lines.append(f"- 受影响模块：{'、'.join(mod_parts)}")
        if len(affected_fps) > 8:
            desc_lines.append(f"- 另有 {len(affected_fps) - 8} 个间接功能点未逐一列出")
        description = "\n".join(desc_lines)

        test_focus = (
            f"重点回归受影响功能点（{affected_fps[0].get('fp_name', '')} 等）的 happy_path "
            "和关键 alternate_flows，并验证与「"
            + fp_name
            + "」交互的边界数据是否兼容新逻辑。"
            if affected_fps
            else "重点验证所属模块内的关联流程与数据兼容性。"
        )
        return description, test_focus

    # ========================================================================
    # v1.7: 需求级影响分析（模块+FP 依赖全景 + KB 上下文 + 人工审核）
    # ========================================================================

    async def analyze_requirement_impact(
        self,
        req_id: str,
        session,
    ) -> dict:
        """对一条需求做全量影响分析（v2.35: 基于 FP 依赖图谱分析受影响功能点）。

        v2.35 变更：
            - 移除 affected_requirements（不再分析受影响的需求）
            - KB 查询改为只查已归档 FP（status='archived'），使用向量库 doc_type=functional_point 过滤
            - 修复 TestCase.functional_point_id 字段不存在 bug（改为按 case_json.related_fp 名称匹配）
            - 保留模块级影响分析作为参考

        Args:
            req_id: 需求 ID
            session: 数据库会话

        Returns:
            dict: 含 requirement / module_impact / affected_fps / total_affected
        """
        from sqlalchemy import func
        from app.db.milvus_store import vector_store

        # 1. 加载需求和 FP
        req_data = await session.get(Requirement, req_id)
        if not req_data:
            return {"error": f"需求 {req_id} 不存在"}

        srr = req_data.srr_json or {}
        involved_modules = list(set((req_data.modules or [])[:]))

        fp_result = await session.execute(
            select(FunctionalPoint).where(FunctionalPoint.requirement_id == req_id)
        )
        own_fps = fp_result.scalars().all()
        own_fp_ids = {fp.id for fp in own_fps}
        # v3.2: 需求自身的功能点名称集合——跨版本同名功能点属于同一功能（视为需求内部），
        # 不纳入"受影响的其他功能"。避免把"工单查询"等同名功能点误报为受影响功能。
        own_fp_names = {fp.name for fp in own_fps}

        # 2. 模块 BFS（保留模块级影响作为参考）
        mod_impact = await self._analyze_module_impact(involved_modules, session) if involved_modules else {
            "direct_modules": [], "indirect_modules": [], "all_affected_modules": [],
        }

        # 3. FP 反向 BFS（基于 FP 依赖图谱分析受影响的功能点）
        all_affected_fps: dict[str, dict] = {}
        for fp in own_fps:
            try:
                reverse_fps = await self._bfs_fp_reverse(fp.id, session)
                for rf in reverse_fps:
                    fid = rf["fp_id"]
                    # v3.2: 双重排除需求自身功能点：
                    #   1) 按 fp_id（本需求直接拥有）
                    #   2) 按 fp_name（跨版本同名功能点属同一功能，视为需求内部，如"工单查询"）
                    if fid not in own_fp_ids and rf.get("fp_name") not in own_fp_names and fid not in all_affected_fps:
                        all_affected_fps[fid] = {
                            "fp_id": rf["fp_id"],
                            "fp_name": rf["fp_name"],
                            # v3.2: 补充归属需求信息（前端展开行展示/取 CRSR 用）
                            "req_id": rf.get("req_id", ""),
                            "req_title": rf.get("req_title", ""),
                            "reason": f"依赖 FP「{fp.name}」（{rf.get('relation_type','')}），深度 {rf['depth']}",
                            "depth": rf["depth"],
                            "source_fp_id": fp.id,
                            "source_fp_name": fp.name,
                        }
            except Exception as e:
                logger.warning("[ImpactAgent] FP BFS 失败 fp={}: {}", fp.id, str(e)[:100])

        # 4. 补充 FP 状态、模块名（用于后续 KB 查询和展示）
        fp_ids_list = list(all_affected_fps.keys())
        fp_mod_map: dict[str, str] = {}
        fp_status_map: dict[str, str] = {}
        if fp_ids_list:
            mr = await session.execute(
                select(FunctionalPoint.id, FunctionalPoint.module_id, FunctionalPoint.status)
                .where(FunctionalPoint.id.in_(fp_ids_list))
            )
            for row in mr.all():
                fp_mod_map[row[0]] = row[1] or ""
                fp_status_map[row[0]] = row[2] or ""
        mod_ids = {v for v in fp_mod_map.values() if v}
        mod_name_map: dict[str, str] = {}
        if mod_ids:
            mn = await session.execute(select(Module.id, Module.name).where(Module.id.in_(mod_ids)))
            mod_name_map = {row[0]: row[1] for row in mn.all()}

        # v3.3 P0-1 引用热度（Obsidian 借鉴）：统计受影响 FP 的扇入（被多少功能点依赖）。
        # 口径与影响分析 BFS 一致：approved + active + 同产品（join requirements 过滤，
        # 防止跨产品数据泄漏）。fan_in 越高 = 变更该 FP 牵连面越大 = 回归优先级越高。
        fan_in_map: dict[str, int] = {}
        if fp_ids_list:
            try:
                from sqlalchemy import func as sa_func
                fan_result = await session.execute(
                    select(FPDependency.target_fp_id, sa_func.count(FPDependency.id))
                    .join(FunctionalPoint, FunctionalPoint.id == FPDependency.source_fp_id)
                    .join(Requirement, Requirement.id == FunctionalPoint.requirement_id)
                    .where(
                        FPDependency.target_fp_id.in_(fp_ids_list),
                        FPDependency.review_status == "approved",
                        FPDependency.is_active == True,  # noqa: E712
                        Requirement.product_id == req_data.product_id,
                    )
                    .group_by(FPDependency.target_fp_id)
                )
                fan_in_map = {row[0]: int(row[1]) for row in fan_result.all()}
            except Exception as e:
                # 扇入统计失败不阻断影响分析（降级为不排序），仅记录日志
                logger.warning("[ImpactAgent] 扇入统计失败（降级不排序）: {}", str(e)[:150])

        # 5. KB 查询：只对已归档（status='archived'）的 FP 查询知识库
        # v2.35: 使用 vector_store.search_similar_requirements 按 doc_type=functional_point 过滤
        # 已归档 FP 在归档时已写入向量库（type=functional_point），可直接检索其说明
        kb_contexts: dict[str, str] = {}
        for fid, fp_info in list(all_affected_fps.items())[:10]:
            # 仅已归档 FP 查询 KB（未归档 FP 未写入向量库，查询无意义）
            if fp_status_map.get(fid) != "archived":
                continue
            try:
                kb_hits = vector_store.search_similar_requirements(
                    query=f"{fp_info['fp_name']} 功能 规格 规则",
                    doc_type="functional_point",
                    status="archived",
                    top_k=2,
                )
                if kb_hits:
                    texts = []
                    for h in kb_hits[:2]:
                        # 优先从 metadata.srr_json 提取结构化说明，其次用 document 文本
                        meta = h.get("metadata", {}) or {}
                        srr_json_str = meta.get("srr_json", "")
                        if srr_json_str:
                            try:
                                src_srr = json.loads(srr_json_str)
                                # 提取 FP 的摘要说明
                                req_info = src_srr.get("requirement", {}) or {}
                                summary = req_info.get("summary", "")
                                if summary:
                                    texts.append(f"功能概述：{summary[:300]}")
                                # 提取 flows
                                flows = src_srr.get("flows", {}) or {}
                                happy = flows.get("happy_path", [])
                                if happy:
                                    texts.append(f"正常流程：{str(happy)[:200]}")
                            except Exception:
                                pass
                        # 兜底：用 document 文本
                        if not texts:
                            t = h.get("document") or h.get("content") or h.get("text") or ""
                            if t:
                                texts.append(t[:500])
                    if texts:
                        kb_contexts[fid] = "\n\n".join(texts)
            except Exception as e:
                logger.warning("[ImpactAgent] KB 查询失败 fp={}: {}", fid, str(e)[:150])

        # 6. 用例数统计（v2.35: 修复 TestCase.functional_point_id 不存在 bug）
        # TestCase 模型无 functional_point_id 外键字段，用例通过 case_json.related_fp（FP 名称）引用
        # 改为查询同产品下用例的 case_json，按 related_fp 名称匹配
        case_counts: dict[str, int] = {}
        if fp_ids_list:
            # 构建 fp_id → fp_name 映射
            fp_name_for_count = {fid: all_affected_fps[fid]["fp_name"] for fid in fp_ids_list}
            # 查询同产品下所有用例（case_json 中含 related_fp 字段）
            case_result = await session.execute(
                select(TestCaseModel.case_json, TestCaseModel.req_id).where(
                    TestCaseModel.product_id == req_data.product_id
                )
            )
            for row in case_result.all():
                case_json = row[0] or {}
                related_fp = case_json.get("related_fp", "") or case_json.get("functional_point", "") or ""
                if not related_fp:
                    continue
                # 按 FP 名称匹配计数
                for fid, fname in fp_name_for_count.items():
                    if related_fp == fname:
                        case_counts[fid] = case_counts.get(fid, 0) + 1

        affected_fps_list = []
        for fid, info in all_affected_fps.items():
            mid = fp_mod_map.get(fid, "")
            affected_fps_list.append({
                "fp_id": info["fp_id"],
                "fp_name": info["fp_name"],
                "fp_status": fp_status_map.get(fid, ""),
                # v3.2: 归属需求（前端展开行跳转/取 CRSR 用）
                "req_id": info.get("req_id", ""),
                "req_title": info.get("req_title", ""),
                "module_id": mid,
                "module_name": mod_name_map.get(mid, ""),
                "impact_reason": info["reason"],
                "depth": info["depth"],
                "source_fp_name": info["source_fp_name"],
                "kb_context": kb_contexts.get(fid, ""),
                "existing_cases_count": case_counts.get(fid, 0),
                # v3.3 P0-1: 引用热度（被依赖数）——回归资源投放优先级依据
                "fan_in": fan_in_map.get(fid, 0),
            })

        # v3.3 P0-1: 按引用热度排序——fan_in 降序（高牵连优先回归），同热度按 BFS 深度升序
        affected_fps_list.sort(key=lambda x: (-x.get("fan_in", 0), x.get("depth", 0)))

        return {
            "requirement": {
                "id": req_id,
                "title": req_data.title,
                "modules": involved_modules,
                "fp_count": len(own_fps),
            },
            "module_impact": {
                "direct": mod_impact.get("direct_modules", []),
                "indirect": mod_impact.get("indirect_modules", []),
            },
            "affected_fps": affected_fps_list,
            "total_affected": len(affected_fps_list),
        }

# 全局单例
impact_agent = ImpactAnalysisAgent()

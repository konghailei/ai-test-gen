"""TestDesignAgent 测试设计 Agent（v2.3 新增）。

依据 docs/00-architecture-overview.md §3.6 与 docs/03-case-generation-flow.md §2.1.0，
在需求分析与用例生成之间增加独立的「测试设计」阶段：

  需求分析（①~④） → ④.5 TestDesignAgent → 用例生成（⑤~⑧）

职责：从 SRR（总 SRR + FP 子 SRR）自动判定可应用的测试设计技术，产出结构化的
测试设计蓝图（blueprint）与定向指导文本，供 GenAgent 在构建 Prompt 时消费。

设计要点：
- 技术扫描口径与 prompts/gen_agent.py:_build_test_design_guidance 保持一致
  （v3.4 系统化测试设计方法论），确保「蓝图结构化汇总」与「既有 Prompt 注入」同源，
  不改变 GenAgent 生成结果的既有结构。
- 8 种测试设计技术（等价类/边界值/决策表/状态迁移/场景/正交/错误推测/因果图）。
- 本 Agent 为纯结构分析（无 LLM 调用），快速、确定、无额外成本。

输入：SRR JSON + 功能点列表
输出：test_design_blueprint dict（结构化）+ 指导文本
"""

from __future__ import annotations

from app.prompts import gen_agent as gen_prompts
from app.utils.logger import logger


class TestDesignAgent:
    """测试设计 Agent。

    从 SRR 结构自动产出测试设计蓝图（技术适用性 + 目标清单 + 定向指导文本）。

    Usage:
        agent = TestDesignAgent()
        blueprint = agent.design(srr, functional_points)
    """

    # 8 种测试设计技术清单（docs/03 §2.1.0 技术编号）
    TECHNIQUES: tuple[str, ...] = (
        "equivalence_partitioning",  # 1 等价类划分法
        "boundary_value",            # 2 边界值分析法
        "decision_table",            # 3 决策表法
        "state_transition",          # 4 状态迁移法
        "scenario",                  # 5 场景法
        "orthogonal_array",          # 6 正交实验法（文档编号）
        "error_guessing",            # 7 错误推测法
        "cause_effect",              # 8 因果图法
    )

    def design(
        self,
        srr: dict,
        functional_points: list[dict] | None = None,
        impact_guidance: str = "",
    ) -> dict:
        """执行测试设计，产出结构化蓝图与定向指导文本。

        Args:
            srr: SRR JSON 字典
            functional_points: 功能点列表（可选，用于 FP 子 SRR 与定向目标识别）
            impact_guidance: 影响分析指导文本（可选，优化需求时传入，仅透传）

        Returns:
            dict: test_design_blueprint，含：
                - techniques_applicable: dict[str, bool]  各技术是否适用
                - boundary_targets: list[str]             等价类+边界值目标字段
                - decision_table_targets: list[str]       决策表目标 FP
                - state_targets: list[str]                状态迁移目标 FP
                - error_guess_hints: list[str]            错误推测检查清单
                - scenario_targets: list[str]             场景法目标
                - guidance_text: str                      注入 GenAgent 的定向指导
        """
        functional_points = functional_points or []
        req_id = (srr or {}).get("requirement", {}).get("id", "")

        # (1) 复用既有技术扫描口径生成定向指导文本（与 GenAgent prompt 注入同源）
        guidance_text = gen_prompts._build_test_design_guidance(srr, functional_points)

        # (2) 结构化汇总技术适用性（从指导文本的组织标题判定）
        techniques = {
            "equivalence_partitioning": "等价类" in guidance_text,
            "boundary_value": "边界值" in guidance_text,
            "decision_table": "决策表" in guidance_text,
            "state_transition": "状态迁移" in guidance_text,
            "scenario": "场景法" in guidance_text,
            "orthogonal_array": "正交" in guidance_text,
            "error_guessing": "错误推测" in guidance_text,
            "cause_effect": "因果图" in guidance_text,
        }

        # (3) 抽取各类目标行（以 bullet 标记开头，用于可观测性展示）
        targets = self._extract_targets(guidance_text)

        blueprint = {
            **targets,
            "techniques_applicable": techniques,
            "guidance_text": guidance_text,
            "impact_guidance_passthrough": bool(impact_guidance),
        }

        # 记录结构化的技术覆盖面（低开销、可观测）
        applicable = [k for k, v in techniques.items() if v]
        logger.info(
            "[TestDesignAgent] 测试设计完成: req_id={}, 适用技术={}, 目标数绑定={boundaries}/{decision}/{states}/{errors}/{scenarios}",
            req_id,
            len(applicable),
            boundaries=len(targets.get("boundary_targets", [])),
            decision=len(targets.get("decision_table_targets", [])),
            states=len(targets.get("state_targets", [])),
            errors=len(targets.get("error_guess_hints", [])),
            scenarios=len(targets.get("scenario_targets", [])),
        )
        return blueprint

    @staticmethod
    def _extract_targets(guidance_text: str) -> dict[str, list[str]]:
        """从定向指导文本中抽取各类目标清单。

        目标行统一以 "  - " 前缀（bullet）开头，按所属技术小节归类。

        Returns:
            dict: boundary_targets / decision_table_targets / state_targets /
                  error_guess_hints / scenario_targets
        """
        sections: dict[str, list[str]] = {
            "boundary_targets": [],
            "decision_table_targets": [],
            "state_targets": [],
            "error_guess_hints": [],
            "scenario_targets": [],
        }
        current = None
        section_keywords = {
            "等价类划分 + 边界值": "boundary_targets",
            "决策表法": "decision_table_targets",
            "状态迁移法": "state_targets",
            "错误推测法": "error_guess_hints",
            "场景法": "scenario_targets",
        }
        for line in guidance_text.splitlines():
            stripped = line.strip()
            matched = next(
                (kw for kw in section_keywords if kw in stripped),
                None,
            )
            if matched:
                current = section_keywords[matched]
                continue
            # 仅保留「实际 SRR 目标」行（前缀含 [fp_id]/[total]/[跨FP集成]），
            # 过滤等价类/边界值等说明性静默行，提升蓝图可观测性。
            if (
                stripped.startswith("- [")
                and current
            ):
                sections[current].append(stripped[2:].strip())
        return sections


# 模块级单例（与 orchestrator 中 gen_agent/clarify_agent 等 Agent 用法保持一致）
test_design_agent = TestDesignAgent()
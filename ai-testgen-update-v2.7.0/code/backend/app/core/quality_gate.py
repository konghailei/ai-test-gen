"""质量门禁（QualityGate）。

在 ClarifyAgent 完成后、用例生成前，对 SRR 执行最终质量检查。
仅从包含明确验收标准的需求生成测试，避免低质量需求进入生成流程
导致产出大量低质量用例。

设计依据：
- docs/01-requirement-parsing.md §5.1（设计理念：约束输入后测试数量下降 20-30%，
  但缺陷检测能力提升）
- docs/01-requirement-parsing.md §5.2（门禁检查项 + 7 项检查 + 动态阈值）
- docs/01-requirement-parsing.md §5.3（门禁流程：Blocker 打回 / Warning 放行 / 全通过进入生成）
- docs/01-requirement-parsing.md §5.4（错误处理与降级策略）

关键设计：
- 动态阈值：冷启动模式（KB 为空或产品首个需求）阈值放宽至 0.60，
  避免冷启动时被门禁反复打回造成死锁（docs/01 §5.2，P2-1 优化项）
- 7 项检查：Schema 校验 / 关键字段非空 / 质量评分达标 / Critical 问题 /
  验收标准明确性(警告) / 步骤数(警告) / 冷启动提示(警告)
- Blocker 阻断通过，Warning 仅标记放行
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.config import settings
from app.models.srr import SRR


# ============================================================================
# 门禁结果模型
# ============================================================================

class QualityGateResult(BaseModel):
    """质量门禁检查结果。

    对应 docs/01 §5.2 check() 方法的返回字典。
    """

    passed: bool = Field(..., description="是否通过门禁（无 Blocker 即通过）")
    blockers: list[str] = Field(
        default_factory=list, description="阻断项列表（非空则 passed=False）"
    )
    warnings: list[str] = Field(
        default_factory=list, description="警告项列表（不阻断，仅提示）"
    )
    pass_score: float = Field(
        default=0.75, description="本次检查使用的通过阈值（冷启动 0.60 / 正常 0.75）"
    )
    cold_start: bool = Field(
        default=False, description="是否为冷启动模式（KB 为空或产品首个需求）"
    )
    recommendation: str = Field(
        default="", description="门禁建议文案：✅ 通过 / ❌ 未通过: <blockers>"
    )


# ============================================================================
# 质量门禁
# ============================================================================

class QualityGate:
    """SRR 质量门禁。

    在 ClarifyAgent 之后、用例生成之前执行最终质量检查。
    对应 docs/01-requirement-parsing.md §5.2 的 RequirementQualityGate。

    检查项（7 项）：
        1. Schema 校验（Blocker）
        2. 关键字段非空（Blocker）：requirement.title / requirement.summary / flows.happy_path
        3. 质量评分达标（Blocker）：quality_report.overall_score >= pass_score
        4. 无未解决的 Critical 问题（Blocker）
        5. 验收标准明确性（Warning）：test_points_hint 充分
        6. happy_path 步骤数（Warning）：>= 2 步
        7. 冷启动模式提示（Warning）：阈值已放宽，建议人工审核

    动态阈值（docs/01 §5.2）：
        - 冷启动模式（kb_empty=True 或 product_first_requirement=True）→ 0.60
        - 正常模式 → 0.75
    """

    # 默认阈值（正常模式），从全局配置读取，便于环境变量覆盖
    DEFAULT_PASS_SCORE: float = settings.DEFAULT_PASS_SCORE

    # 冷启动阈值（KB 为空时放宽），避免冷启动死锁
    COLD_START_PASS_SCORE: float = settings.COLD_START_PASS_SCORE

    def _resolve_pass_score(self, srr: dict, clarify_result: dict) -> tuple[float, bool]:
        """根据上下文动态选择通过分数。

        Args:
            srr: SRR JSON
            clarify_result: ClarifyAgent 的输出，含 kb_status 字段

        Returns:
            tuple[float, bool]: (通过分数, 是否冷启动模式)

        Note:
            冷启动判定（docs/01 §5.2）：
            - kb_status.kb_empty=True：整个知识库为空
            - kb_status.product_first_requirement=True：当前产品下首个需求
            任一为真即进入冷启动模式，阈值放宽至 0.60。
        """
        kb_status = clarify_result.get("kb_status", {})
        if kb_status.get("kb_empty") or kb_status.get("product_first_requirement"):
            return self.COLD_START_PASS_SCORE, True
        return self.DEFAULT_PASS_SCORE, False

    def _validate_schema(self, srr: dict) -> bool:
        """Schema 校验。

        使用 Pydantic SRR 模型校验 SRR 字典是否符合 Schema 规范。
        对应 docs/01 §5.2 检查项 1。

        Args:
            srr: SRR JSON 字典

        Returns:
            bool: True 表示符合 Schema
        """
        try:
            SRR.model_validate(srr)
            return True
        except Exception:
            return False

    def _get_nested_value(self, data: dict, path: str) -> Any:
        """安全获取嵌套字段值。

        Args:
            data: 字典
            path: 点分路径，如 "requirement.title"

        Returns:
            Any: 字段值；路径不存在返回 None
        """
        current: Any = data
        for key in path.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(key)
            if current is None:
                return None
        return current

    async def check(self, srr: dict, clarify_result: dict) -> QualityGateResult:
        """执行质量门禁检查。

        Args:
            srr: SRR JSON 字典（经 ClarifyAgent 细化后的 SRR v2）
            clarify_result: ClarifyAgent 输出，含以下字段：
                - kb_status: {kb_empty, product_first_requirement}
                - quality_report: {overall_score} （可选，无则跳过评分检查）
                - questions: [{priority: "critical"/"important"/"optional"}] （可选）

        Returns:
            QualityGateResult: 门禁结果

        Note:
            检查流程（docs/01 §5.2）：
                0. 动态选择通过阈值（冷启动 0.60 / 正常 0.75）
                1. Schema 校验 → Blocker
                2. 关键字段非空 → Blocker
                3. 质量评分达标 → Blocker
                4. 无未解决 Critical 问题 → Blocker
                5. 验收标准明确性 → Warning
                6. happy_path 步骤数 → Warning
                7. 冷启动模式提示 → Warning
        """
        blockers: list[str] = []
        warnings: list[str] = []

        # 0. 动态选择通过阈值
        pass_score, cold_start = self._resolve_pass_score(srr, clarify_result)

        # 1. Schema 校验
        schema_valid = self._validate_schema(srr)
        if not schema_valid:
            blockers.append("SRR 格式不符合 Schema 规范")

        # 2. 关键字段非空（Schema 校验失败时跳过，避免与检查项 1 产生冗余 blocker）
        if schema_valid:
            for field in ["requirement.title", "requirement.summary", "flows.happy_path"]:
                if not self._get_nested_value(srr, field):
                    blockers.append(f"关键字段 {field} 为空")

        # 3. 质量评分达标（冷启动模式阈值放宽至 0.60）
        quality_report = clarify_result.get("quality_report")
        if quality_report:
            score = quality_report.get("overall_score", 0.0)
            if score < pass_score:
                cold_hint = "（冷启动模式）" if cold_start else ""
                blockers.append(
                    f"质量评分 {score} 低于阈值 {pass_score}{cold_hint}"
                )

        # 4. 无未解决的 Critical 问题
        # 注：出现在 questions 列表中的问题默认即为未解决（已解决的应从列表移除）；
        # resolved 字段为预留兼容字段，默认 False
        questions = clarify_result.get("questions", [])
        if questions:
            critical = [
                q for q in questions
                if q.get("priority") == "critical" and not q.get("resolved", False)
            ]
            if critical:
                blockers.append(
                    f"仍有 {len(critical)} 个 Critical 问题未解决"
                )

        # 5. 验收标准明确性（警告）
        hints = srr.get("test_points_hint", [])

        def _hint_point(h) -> str:
            """安全提取验收标准文本，避免 h 为字符串/非预期类型时崩溃。"""
            if isinstance(h, dict):
                p = h.get("point", "")
                return p if isinstance(p, str) else ""
            return h if isinstance(h, str) else ""

        if not hints or all(len(_hint_point(h)) <= 5 for h in hints):
            warnings.append("验收标准不够明确")

        # 6. happy_path 步骤数（警告）
        steps = self._get_nested_value(srr, "flows.happy_path.steps") or []
        if len(steps) < 2:
            warnings.append("正常流程步骤过少")

        # 7. 冷启动模式提示（警告，非阻断）
        if cold_start:
            warnings.append(
                "冷启动模式：知识库为空或为该产品首个需求，"
                "质量门禁阈值已放宽至 0.60，建议人工重点审核生成结果"
            )

        passed = len(blockers) == 0
        recommendation = "✅ 通过" if passed else f"❌ 未通过: {'; '.join(blockers)}"

        return QualityGateResult(
            passed=passed,
            blockers=blockers,
            warnings=warnings,
            pass_score=pass_score,
            cold_start=cold_start,
            recommendation=recommendation,
        )


# 全局单例
quality_gate = QualityGate()

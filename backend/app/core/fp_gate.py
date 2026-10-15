"""功能点拆分质量门禁（FPGate）。

在 FP 拆分完成后、落库前执行轻量校验，拦截明显异常的拆分结果。
符合项目「错误可见性原则」：FP 拆分失败必须中断流水线、标记任务 failed。

设计原则：
1. 只拦截明确错误，不做质量评分——避免过度设计
2. 失败时中断流水线，标记 task 为 failed
3. 不引入新表，直接在 FP 拆分流程中调用
4. 校验项保持精简，避免阻塞正常流程

检查项：
1. FP 非空：拆分后 FP 列表不能为空（除非需求确实无功能点）
2. FP 字段完整：每个 FP 必须有 name（缺失则阻断）；summary 缺失仅告警（v4.0.3，可由 name 兜底）
3. FP 唯一性：同一需求下 FP name 不能重复（违反唯一约束）
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.utils.logger import logger


class FPGateResult(BaseModel):
    """FP 拆分质量门禁检查结果。"""

    passed: bool = Field(..., description="是否通过门禁（无 blockers 即通过）")
    blockers: list[str] = Field(
        default_factory=list, description="阻断项列表（非空则 passed=False）"
    )
    warnings: list[str] = Field(
        default_factory=list, description="警告项列表（不阻断，仅提示）"
    )
    fp_count: int = Field(default=0, description="FP 数量")


class FPGate:
    """功能点拆分质量门禁。

    在 FP 拆分后、落库前执行轻量校验。
    只拦截明确错误，不做质量评分。
    """

    def check(self, fps: list[dict], srr: dict | None = None) -> FPGateResult:
        """执行 FP 拆分质量门禁检查。

        Args:
            fps: FP 字典列表（来自 _fp_extract_from_raw）
            srr: SRR 字典（用于判断需求是否有功能点，可选）

        Returns:
            FPGateResult: 门禁结果

        Note:
            检查项：
                1. FP 非空（Blocker）：fps 列表为空时阻断
                   - 例外：如果 SRR 明确标记无功能点（如纯数据约束需求），放行
                2. FP 字段完整（Blocker）：每个 FP 必须有 name 和 summary
                3. FP 唯一性（Blocker）：同一需求下 FP name 不能重复
        """
        blockers: list[str] = []
        warnings: list[str] = []

        # 1. FP 非空检查
        if not fps:
            # 例外判断：SRR 明确标记无功能点
            if srr and self._is_no_fp_requirement(srr):
                warnings.append("需求标记为无功能点，FP 列表为空（设计如此）")
                return FPGateResult(
                    passed=True,
                    blockers=[],
                    warnings=warnings,
                    fp_count=0,
                )
            blockers.append("FP 拆分结果为空，但需求未标记为无功能点")
            return FPGateResult(
                passed=False,
                blockers=blockers,
                warnings=warnings,
                fp_count=0,
            )

        # 2. FP 字段完整检查
        missing_name: list[int] = []
        missing_summary: list[int] = []
        for idx, fp in enumerate(fps):
            if not fp.get("name") or not str(fp.get("name", "")).strip():
                missing_name.append(idx)
            # summary 可以为空字符串但字段必须存在；完全缺失则视为异常
            if "summary" not in fp:
                missing_summary.append(idx)

        if missing_name:
            blockers.append(
                f"{len(missing_name)} 个 FP 缺少 name 字段（索引: {missing_name[:5]}）"
            )
        # v4.0.3 修复（自证 T2）：summary 字段缺失从 Blocker 降为 WARNING。
        # 理由：name 是 FP 的身份标识（缺失=真错误，必须拦）；summary 是描述性字段，
        # 缺失时可由 name 兜底自动补全，不应中断整条解析流水线（曾导致澄清重生成熔断）。
        if missing_summary:
            warnings.append(
                f"{len(missing_summary)} 个 FP 缺少 summary 字段（索引: {missing_summary[:5]}），"
                f"已按 name 兜底处理"
            )

        # 3. FP 唯一性检查（同一需求下 name 不能重复）
        seen_names: dict[str, int] = {}
        duplicates: list[str] = []
        for fp in fps:
            name = str(fp.get("name", "")).strip()
            if not name:
                continue
            if name in seen_names:
                duplicates.append(name)
            else:
                seen_names[name] = 1

        if duplicates:
            blockers.append(
                f"FP name 重复（共 {len(duplicates)} 个）: {duplicates[:5]}"
            )

        # 4. 警告：FP 数量异常
        fp_count = len(fps)
        if fp_count > 30:
            warnings.append(
                f"FP 数量较多（{fp_count} 个），建议检查是否拆分过细"
            )
        elif fp_count == 1:
            # 单个 FP 不一定是问题，但值得提示
            warnings.append("仅 1 个 FP，请确认是否需要进一步拆分")

        passed = len(blockers) == 0
        result = FPGateResult(
            passed=passed,
            blockers=blockers,
            warnings=warnings,
            fp_count=fp_count,
        )

        logger.info(
            "[FPGate] 检查完成: passed={}, fp_count={}, blockers={}, warnings={}",
            passed, fp_count, len(blockers), len(warnings),
        )
        return result

    def _is_no_fp_requirement(self, srr: dict) -> bool:
        """判断 SRR 是否明确标记为无功能点需求。

        某些纯数据约束、配置变更、文档修订类需求确实无功能点，
        此时 FP 为空是设计选择而非缺失。

        Args:
            srr: SRR 字典

        Returns:
            bool: True 表示需求明确无功能点
        """
        if not isinstance(srr, dict):
            return False

        # 检查 SRR 的 requirement 字段是否标记无功能点
        req = srr.get("requirement", {}) or {}
        if req.get("no_functional_points") is True:
            return True

        # 检查 fp_type 是否为 config/data_record（这些类型可能无 workflow）
        # 但这不能完全确定无 FP，保守返回 False
        return False


# 全局单例
fp_gate = FPGate()

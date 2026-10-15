"""用例类型分类体系（v3.5.11，docs/04 类型体系定稿，大哥拍板 2026-10-15）。

## 单一权威：细分 type → 展示分组

细分 type（8+2 值）是 **LLM 输出契约与 DB 存量字段**，永不变；本模块定义
它到"展示分组"的映射，供前端显示 / xmind / markdown 导出统一消费。
**禁止各处再写映射表**（历史上 5 处各自为政已漂移："边界值" vs "边界测试"、
markdown 缺 regression——收敛于此）。

## 分组设计（经典测试分类对齐）

- **功能测试组 functional**：functional + boundary（边界值分析）+ exception
  （错误推测/负面测试）+ exploratory（探索性）。经典理论上三者 ⊂ 功能测试
  （设计技术与风格），系统内部保留细分是为让"技术覆盖矩阵"可度量
  （docs/13 §183-184：等价类划分/边界值/错误推测法直接数细分 type 分布）。
- **场景测试组 scenario**：integration 展示更名（内部值不变）——跨功能点
  业务场景/流程（orchestrator 跨 FP 通道归一化产出），非经典"模块接口
  集成测试"。
- **安全/性能/回归**：独立分组（非功能质量特性 / 测试目的）。
- **UI/接口/数据/其他**：v2 时代手工录入遗留细分，归入同名分组。

## 前端镜像

`frontend/src/utils/caseType.ts` 是本模块的 TS 镜像（同构同序），
两边单测锁定；跨语言无法物理共享一份代码，但保持结构单一来源。
"""

from __future__ import annotations

# 细分 type → 分组 key（LLM 契约/DB 值 → 展示组）
TYPE_GROUP: dict[str, str] = {
    # 功能测试组（经典归属：功能质量特性下的设计技术与风格）
    "functional": "functional",
    "boundary": "functional",
    "exception": "functional",
    "exploratory": "functional",
    # 场景测试组（跨功能点业务场景；内部值沿用 integration，仅展示更名）
    "integration": "scenario",
    # 独立分组
    "security": "security",
    "performance": "performance",
    "regression": "regression",
    # 遗留细分（v2 时代手工录入用例）
    "ui": "ui",
    "api": "api",
    "data": "data",
}

# 分组中文名（展示口径）
GROUP_LABEL: dict[str, str] = {
    "functional": "功能测试",
    "scenario": "场景测试",
    "security": "安全测试",
    "performance": "性能测试",
    "regression": "回归测试",
    "ui": "界面测试",
    "api": "接口测试",
    "data": "数据测试",
    "other": "其他",
}

# 细分中文名（复合标签的细分部分用）
DETAIL_LABEL: dict[str, str] = {
    "functional": "功能",
    "boundary": "边界值",
    "exception": "异常",
    "exploratory": "探索性",
    "integration": "跨功能点场景",
    "security": "安全",
    "performance": "性能",
    "regression": "回归",
    "ui": "UI",
    "api": "接口",
    "data": "数据",
    "other": "其他",
}

# 分组展示顺序（xmind 分组节点 / 前端筛选用）
GROUP_ORDER: tuple[str, ...] = (
    "functional", "scenario", "security", "performance",
    "regression", "ui", "api", "data", "other",
)

# 组内细分顺序（复合标签与组内排序用）
_DETAIL_ORDER: dict[str, tuple[str, ...]] = {
    "functional": ("functional", "boundary", "exception", "exploratory"),
    "scenario": ("integration",),
    "security": ("security",),
    "performance": ("performance",),
    "regression": ("regression",),
    "ui": ("ui",),
    "api": ("api",),
    "data": ("data",),
    "other": ("other",),
}

# 每组"代表值"：type 等于代表值时复合标签直接显示组名（不重复缀细分）
_GROUP_REPRESENTATIVE: dict[str, str] = {
    "functional": "functional",
    "scenario": "integration",
    "security": "security",
    "performance": "performance",
    "regression": "regression",
    "ui": "ui",
    "api": "api",
    "data": "data",
    "other": "other",
}


def case_type_group(t: str | None) -> str:
    """细分 type → 分组 key；未知值归 other（前端/导出不显示英文原值）。"""
    key = (t or "").strip().lower()
    return TYPE_GROUP.get(key, "other")


def case_type_group_label(t: str | None) -> str:
    """细分 type → 分组中文名（如 boundary → '功能测试'）。"""
    return GROUP_LABEL.get(case_type_group(t), "其他")


def case_type_detail_label(t: str | None) -> str:
    """细分 type → 细分中文名（如 boundary → '边界值'）。"""
    key = (t or "").strip().lower()
    return DETAIL_LABEL.get(key, key or "其他")


def case_type_composite_label(t: str | None) -> str:
    """复合标签：组内多细分的显示'功能测试·边界值'；代表值直接显示组名。

    用于表格"类型"列、标签徽章等需要一眼看清归属又不丢细分信息的场景。
    空值（脏数据缺 type）与代表值同形态：只显示组名，不缀'其他'。
    """
    g = case_type_group(t)
    group_label = GROUP_LABEL.get(g, "其他")
    key = (t or "").strip().lower()
    if not key or key == _GROUP_REPRESENTATIVE.get(g):
        return group_label
    return f"{group_label}·{case_type_detail_label(t)}"


def group_representative(group_key: str) -> str:
    """分组 → 代表细分 type（分组节点下新建用例时的兜底 type）。"""
    return _GROUP_REPRESENTATIVE.get(group_key, "other")


def group_members(group_key: str) -> tuple[str, ...]:
    """分组 → 组内细分 type 列表（按展示顺序）。"""
    return _DETAIL_ORDER.get(group_key, (group_key,))


def group_sort_key(group_key: str) -> int:
    """分组排序键（GROUP_ORDER 下标；未知分组排最后）。"""
    try:
        return GROUP_ORDER.index(group_key)
    except ValueError:
        return len(GROUP_ORDER)

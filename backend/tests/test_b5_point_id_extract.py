"""B-5 要点 ID 提取假阳性修正（docs/32 §B-5，docs/31 F2）。

两类假阳性（v3.5.9 三版本实测）：
1. 标题裸词型：正文写"…happy_path 验证"，裸类型词被当覆盖声明提取
   → invalid_point_ids=["happy_path"]；
2. URL 提取型：description 里的 "/api/users"，`api` 被裸正则捞出
   → invalid_point_ids=["api"]。

修法（确定性）：
- 裸正则索引后缀必选（真实要点 ID 必带 .{i}，仅 non_functional 允许 .{key}）
  ——裸类型词任何位置都不再构成声明；
- lookbehind/lookahead 补 `/`、`:`，排除 URL/路径形态；
- 方括号权威形态与结构化字段提取不受影响（合法声明零误伤）。
"""
from __future__ import annotations

from app.utils.coverage import extract_point_ids


def test_bare_type_name_in_prose_not_extracted():
    """docs/31 F2 形态 1：正文裸类型词不再是覆盖声明。"""
    case = {
        "title": "原型HTML语义树解析 happy_path 验证",
        "description": "覆盖 api 与 role 的场景",
        "steps": [{"action": "执行检查", "expected": "通过 rule 校验"}],
    }
    assert extract_point_ids(case) == set(), "裸类型词不得提取为要点 ID"


def test_bare_suffixed_ids_still_extracted():
    """带索引后缀的裸 ID 是合法声明形态（既有行为，回归锁定）。"""
    case = {
        "title": "验证 fp_login.rule.1",
        "steps": [{"action": "检查 alternate_flow.2", "expected": "满足 shared_rule.1"}],
        "notes": "另见 fp_x.rule.2 与 non_functional.performance",
    }
    assert extract_point_ids(case) == {
        "fp_login.rule.1", "alternate_flow.2", "shared_rule.1",
        "fp_x.rule.2", "non_functional.performance",
    }


def test_url_and_path_forms_not_extracted():
    """docs/31 F2 形态 2：URL/路径/端口形态不提取 api。"""
    for text in (
        "请求 /api/users 返回 200",
        "服务监听 api:8080",
        "路径 api/v1/items",
        "前缀/api/后缀",
    ):
        case = {"description": text}
        assert extract_point_ids(case) == set(), f"URL 形态误提: {text!r}"


def test_bracketed_authoritative_form_untouched():
    """方括号权威形态照常提取（含无索引的短形态）。"""
    case = {
        "title": "校验 [happy_path.1] 与 [fp_a.data.2]",
        "notes": "覆盖: [happy_path] [role.3]",
    }
    assert extract_point_ids(case) == {
        "happy_path.1", "fp_a.data.2", "happy_path", "role.3",
    }


def test_structured_fields_untouched():
    """结构化字段提取不受正则修正影响。"""
    case = {"covered_point_ids": ["happy_path.1", "rule.2"], "notes": "[data.3]"}
    assert extract_point_ids(case) == {"happy_path.1", "rule.2", "data.3"}

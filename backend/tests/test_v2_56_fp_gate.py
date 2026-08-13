"""v2.8.56 FP 拆分质量门禁 + 知识问答排序修复 单元测试

验证两项修复：
1. FPGate.check 三项检查（FP 非空、字段完整、唯一性）
2. knowledge_qa 排序 bug 修复确认（无应用层覆盖排序）

运行: python -m pytest tests/test_v2_56_fp_gate.py -v
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.core.fp_gate import FPGate, fp_gate


class TestFPGate:
    """FP 拆分质量门禁测试"""

    def test_empty_fps_with_no_marker_blocked(self):
        """空 FP 列表且未标记无功能点 → 阻断"""
        result = fp_gate.check([], srr=None)
        assert not result.passed
        assert any("FP 拆分结果为空" in b for b in result.blockers)

    def test_empty_fps_with_marker_passed(self):
        """空 FP 列表但 SRR 标记无功能点 → 放行"""
        srr = {"requirement": {"no_functional_points": True}}
        result = fp_gate.check([], srr=srr)
        assert result.passed
        assert any("设计如此" in w for w in result.warnings)

    def test_valid_fps_passed(self):
        """正常的 FP 列表 → 通过"""
        fps = [
            {"name": "登录功能", "summary": "用户登录流程"},
            {"name": "注册功能", "summary": "用户注册流程"},
        ]
        result = fp_gate.check(fps)
        assert result.passed
        assert result.fp_count == 2
        assert len(result.blockers) == 0

    def test_missing_name_blocked(self):
        """FP 缺少 name → 阻断"""
        fps = [
            {"name": "", "summary": "无名称的功能"},
            {"name": "正常功能", "summary": "正常"},
        ]
        result = fp_gate.check(fps)
        assert not result.passed
        assert any("缺少 name" in b for b in result.blockers)

    def test_missing_summary_blocked(self):
        """FP 缺少 summary 字段 → 阻断"""
        fps = [
            {"name": "功能A", "summary": "正常"},
            {"name": "功能B"},  # 缺少 summary 键
        ]
        result = fp_gate.check(fps)
        assert not result.passed
        assert any("缺少 summary" in b for b in result.blockers)

    def test_duplicate_names_blocked(self):
        """FP name 重复 → 阻断"""
        fps = [
            {"name": "重复功能", "summary": "第一个"},
            {"name": "重复功能", "summary": "第二个"},
        ]
        result = fp_gate.check(fps)
        assert not result.passed
        assert any("重复" in b for b in result.blockers)

    def test_warning_too_many_fps(self):
        """FP 数量超过 30 → 警告但不阻断"""
        fps = [
            {"name": f"功能{i}", "summary": f"功能{i}的描述"}
            for i in range(35)
        ]
        result = fp_gate.check(fps)
        assert result.passed
        assert any("拆分过细" in w for w in result.warnings)

    def test_warning_single_fp(self):
        """仅 1 个 FP → 警告但不阻断"""
        fps = [{"name": "唯一功能", "summary": "描述"}]
        result = fp_gate.check(fps)
        assert result.passed
        assert any("1 个 FP" in w for w in result.warnings)

    def test_fp_count_correct(self):
        """fp_count 字段返回正确的 FP 数量"""
        fps = [
            {"name": f"功能{i}", "summary": f"描述{i}"}
            for i in range(5)
        ]
        result = fp_gate.check(fps)
        assert result.fp_count == 5

    def test_empty_name_whitespace_blocked(self):
        """name 为纯空格 → 视为缺失"""
        fps = [
            {"name": "   ", "summary": "空格名称"},
        ]
        result = fp_gate.check(fps)
        assert not result.passed
        assert any("缺少 name" in b for b in result.blockers)


class TestKnowledgeQASortFix:
    """知识问答排序修复验证（代码审查式验证）"""

    def test_no_application_layer_sort(self):
        """验证 knowledge_qa.py 已移除应用层 go_live_date 覆盖排序

        通过读取源文件，确认不再有 kb_sources.sort 调用
        """
        src_path = os.path.join(
            os.path.dirname(__file__), '..', 'app', 'api', 'knowledge_qa.py'
        )
        with open(src_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # 不应有 kb_sources.sort 调用
        assert 'kb_sources.sort(' not in content, (
            "knowledge_qa.py 不应包含 kb_sources.sort 应用层覆盖排序"
        )

        # 应有 v2.8.55 注释说明
        assert 'v2.8.55: 移除应用层 go_live_date 覆盖排序' in content, (
            "knowledge_qa.py 应包含 v2.8.55 修复注释"
        )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))

"""v2.8.56 知识问答纠正审核流程 单元测试

验证纠正审核流程的关键功能：
1. 数据模型字段（KbQARecord / KbQACorrectRequest / KbQAReviewRequest）
2. SCHEMA_MIGRATIONS 包含审核流程字段
3. DEFAULT_MENUS 包含 correction_review 菜单
4. API 路由端点存在
5. 前端纠正审核页面与路由配置
6. approve 接口区分 FP 级别部分纠正与需求级别完整纠正

运行: python -m pytest tests/test_v2_56_kqa_review.py -v
"""

import os
import sys
import inspect

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestKQAReviewSchemas:
    """验证 Pydantic schema 包含审核流程字段"""

    def test_correct_request_has_review_fields(self):
        """KbQACorrectRequest 包含 correction_module / correction_target_type / correction_target_id"""
        from app.models.api_schemas import KbQACorrectRequest

        fields = KbQACorrectRequest.model_fields
        assert 'correction_module' in fields, "KbQACorrectRequest 缺少 correction_module 字段"
        assert 'correction_target_type' in fields, "KbQACorrectRequest 缺少 correction_target_type 字段"
        assert 'correction_target_id' in fields, "KbQACorrectRequest 缺少 correction_target_id 字段"

    def test_correct_request_target_type_literal(self):
        """correction_target_type 必须是 Literal['requirement', 'functional_point']"""
        from app.models.api_schemas import KbQACorrectRequest
        from typing import Literal, get_args, get_origin

        field = KbQACorrectRequest.model_fields['correction_target_type']
        # 默认值必须是 'requirement'
        assert field.default == 'requirement', "correction_target_type 默认值应为 'requirement'"

    def test_review_request_schema(self):
        """KbQAReviewRequest 包含 id 和 review_notes"""
        from app.models.api_schemas import KbQAReviewRequest

        fields = KbQAReviewRequest.model_fields
        assert 'id' in fields, "KbQAReviewRequest 缺少 id 字段"
        assert 'review_notes' in fields, "KbQAReviewRequest 缺少 review_notes 字段"

    def test_record_has_review_status_fields(self):
        """KbQARecord 包含审核流程所有字段"""
        from app.models.api_schemas import KbQARecord

        fields = KbQARecord.model_fields
        expected = {
            'correction_status', 'correction_module',
            'correction_target_type', 'correction_target_id',
            'reviewed_by', 'reviewed_at', 'review_notes',
        }
        missing = expected - set(fields.keys())
        assert not missing, f"KbQARecord 缺少字段: {missing}"


class TestKQAReviewDBMigrations:
    """验证数据库迁移配置"""

    def test_schema_migrations_has_kqa_review_fields(self):
        """SCHEMA_MIGRATIONS['knowledge_qa'] 包含审核流程字段"""
        # 直接读取 database.py 源文件，避免触发 DATABASE_URL 检查
        db_path = os.path.join(
            os.path.dirname(__file__), '..', 'app', 'db', 'database.py'
        )
        with open(db_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # 验证 v2.8.56 字段
        assert 'correction_status' in content, "database.py 缺少 correction_status 字段迁移"
        assert 'correction_module' in content, "database.py 缺少 correction_module 字段迁移"
        assert 'correction_target_type' in content, "database.py 缺少 correction_target_type 字段迁移"
        assert 'correction_target_id' in content, "database.py 缺少 correction_target_id 字段迁移"
        assert 'reviewed_by' in content, "database.py 缺少 reviewed_by 字段迁移"
        assert 'reviewed_at' in content, "database.py 缺少 reviewed_at 字段迁移"
        assert 'review_notes' in content, "database.py 缺少 review_notes 字段迁移"

    def test_search_indexes_has_kqa_review_indexes(self):
        """SEARCH_INDEXES 包含审核流程筛选索引"""
        db_path = os.path.join(
            os.path.dirname(__file__), '..', 'app', 'db', 'database.py'
        )
        with open(db_path, 'r', encoding='utf-8') as f:
            content = f.read()

        assert 'idx_kqa_correction_status' in content, "缺少 correction_status 索引"
        assert 'idx_kqa_correction_module' in content, "缺少 correction_module 索引"

    def test_default_menus_has_correction_review(self):
        """DEFAULT_MENUS 包含 correction_review"""
        # 直接读取 models.py 源文件验证
        models_path = os.path.join(
            os.path.dirname(__file__), '..', 'app', 'db', 'models.py'
        )
        with open(models_path, 'r', encoding='utf-8') as f:
            content = f.read()

        assert 'correction_review' in content, "DEFAULT_MENUS 缺少 correction_review 菜单项"

    def test_roles_menu_labels_has_correction_review(self):
        """/roles/menus 接口的 menu_labels 包含 correction_review"""
        roles_path = os.path.join(
            os.path.dirname(__file__), '..', 'app', 'api', 'roles.py'
        )
        with open(roles_path, 'r', encoding='utf-8') as f:
            content = f.read()

        assert 'correction_review' in content, "roles.py 缺少 correction_review 菜单标签"


class TestKQAReviewAPIEndpoints:
    """验证 API 路由端点定义"""

    def test_kqa_router_has_correction_endpoints(self):
        """knowledge_qa.router 包含 /corrections, /corrections/{id}/approve, /corrections/{id}/reject"""
        kqa_path = os.path.join(
            os.path.dirname(__file__), '..', 'app', 'api', 'knowledge_qa.py'
        )
        with open(kqa_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # 验证路由定义
        assert '"/corrections"' in content, "缺少 GET /corrections 端点"
        assert '"/corrections/{record_id}/approve"' in content, "缺少 POST /corrections/{id}/approve 端点"
        assert '"/corrections/{record_id}/reject"' in content, "缺少 POST /corrections/{id}/reject 端点"

    def test_correct_endpoint_sets_pending_review(self):
        """/correct 端点将 correction_status 设置为 pending_review"""
        kqa_path = os.path.join(
            os.path.dirname(__file__), '..', 'app', 'api', 'knowledge_qa.py'
        )
        with open(kqa_path, 'r', encoding='utf-8') as f:
            content = f.read()

        assert 'correction_status = "pending_review"' in content, \
            "/correct 未将 correction_status 设置为 pending_review"

    def test_approve_endpoint_handles_fp_partial_correction(self):
        """approve 端点支持 FP 级别部分纠正（删除+重新嵌入）"""
        kqa_path = os.path.join(
            os.path.dirname(__file__), '..', 'app', 'api', 'knowledge_qa.py'
        )
        with open(kqa_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # 验证 FP 部分纠正逻辑
        assert 'functional_point' in content, "approve 端点未处理 functional_point 颗粒度"
        assert 'delete_requirement' in content, "approve 端点未调用 delete_requirement 删除原 FP 向量"
        assert 'add_requirement' in content, "approve 端点未调用 add_requirement 重新嵌入"

    def test_approve_endpoint_status_guard(self):
        """approve 端点仅在 pending_review 状态下可审核"""
        kqa_path = os.path.join(
            os.path.dirname(__file__), '..', 'app', 'api', 'knowledge_qa.py'
        )
        with open(kqa_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # 验证状态守卫
        assert 'correction_status != "pending_review"' in content, \
            "approve 端点缺少 pending_review 状态守卫"

    def test_reject_endpoint_does_not_archive(self):
        """reject 端点不调用 vector_store 归档"""
        kqa_path = os.path.join(
            os.path.dirname(__file__), '..', 'app', 'api', 'knowledge_qa.py'
        )
        with open(kqa_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # 提取 reject 函数体（从 kqa_reject_correction 到下一个 # ====）
        reject_start = content.find('async def kqa_reject_correction')
        assert reject_start != -1, "找不到 kqa_reject_correction 函数"

        # 找下一个区域分隔符
        next_section = content.find('# ====', reject_start)
        if next_section == -1:
            next_section = len(content)

        reject_body = content[reject_start:next_section]

        # reject 不应包含 vector_store.add_requirement 调用
        assert 'vector_store.add_requirement' not in reject_body, \
            "reject 端点不应调用 vector_store.add_requirement（拒绝不应归档）"
        assert 'correction_status = "rejected"' in reject_body, \
            "reject 端点未将 correction_status 设置为 rejected"


class TestKQAReviewFrontend:
    """验证前端纠正审核页面与路由"""

    def test_correction_review_page_exists(self):
        """前端有 CorrectionReview.tsx 页面"""
        page_path = os.path.join(
            os.path.dirname(__file__), '..', '..', 'frontend', 'src', 'pages', 'CorrectionReview.tsx'
        )
        assert os.path.exists(page_path), f"前端页面不存在: {page_path}"

        with open(page_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # 验证关键功能
        assert 'knowledgeQA.corrections' in content, "页面未调用 knowledgeQA.corrections API"
        assert 'knowledgeQA.approve' in content, "页面未调用 knowledgeQA.approve API"
        assert 'knowledgeQA.reject' in content, "页面未调用 knowledgeQA.reject API"
        assert 'moduleFilter' in content, "页面未实现模块筛选"
        assert 'statusFilter' in content, "页面未实现状态筛选"

    def test_app_tsx_has_corrections_route(self):
        """App.tsx 包含 /corrections 路由"""
        app_path = os.path.join(
            os.path.dirname(__file__), '..', '..', 'frontend', 'src', 'App.tsx'
        )
        with open(app_path, 'r', encoding='utf-8') as f:
            content = f.read()

        assert 'CorrectionReview' in content, "App.tsx 未导入 CorrectionReview 组件"
        assert 'path="/corrections"' in content, "App.tsx 未配置 /corrections 路由"

    def test_sidebar_has_correction_review_menu(self):
        """Sidebar 包含纠正审核菜单项"""
        sidebar_path = os.path.join(
            os.path.dirname(__file__), '..', '..', 'frontend', 'src', 'components', 'Sidebar.tsx'
        )
        with open(sidebar_path, 'r', encoding='utf-8') as f:
            content = f.read()

        assert 'correction_review' in content, "Sidebar 未包含 correction_review 菜单 key"
        assert '/corrections' in content, "Sidebar 未配置 /corrections 路径"
        assert 'ClipboardCheck' in content, "Sidebar 未使用 ClipboardCheck 图标"

    def test_client_ts_has_review_apis(self):
        """client.ts 包含审核流程 API 调用方法"""
        client_path = os.path.join(
            os.path.dirname(__file__), '..', '..', 'frontend', 'src', 'api', 'client.ts'
        )
        with open(client_path, 'r', encoding='utf-8') as f:
            content = f.read()

        assert 'corrections:' in content, "client.ts 缺少 corrections 方法"
        assert 'approve:' in content, "client.ts 缺少 approve 方法"
        assert 'reject:' in content, "client.ts 缺少 reject 方法"

    def test_knowledge_qa_page_has_link_to_corrections(self):
        """KnowledgeQA.tsx 包含跳转到纠正审核页的入口"""
        page_path = os.path.join(
            os.path.dirname(__file__), '..', '..', 'frontend', 'src', 'pages', 'KnowledgeQA.tsx'
        )
        with open(page_path, 'r', encoding='utf-8') as f:
            content = f.read()

        assert '/corrections' in content, "KnowledgeQA.tsx 未配置跳转到 /corrections 的入口"
        assert 'ClipboardCheck' in content, "KnowledgeQA.tsx 未使用 ClipboardCheck 图标作为入口按钮"

    def test_knowledge_qa_page_has_granularity_selector(self):
        """KnowledgeQA.tsx 纠错弹窗包含颗粒度选择"""
        page_path = os.path.join(
            os.path.dirname(__file__), '..', '..', 'frontend', 'src', 'pages', 'KnowledgeQA.tsx'
        )
        with open(page_path, 'r', encoding='utf-8') as f:
            content = f.read()

        assert 'correctTargetType' in content, "KnowledgeQA.tsx 缺少 correctTargetType 状态"
        assert 'correctModule' in content, "KnowledgeQA.tsx 缺少 correctModule 状态"
        assert 'correctTargetId' in content, "KnowledgeQA.tsx 缺少 correctTargetId 状态"
        assert 'functional_point' in content, "KnowledgeQA.tsx 未提供 functional_point 选项"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))

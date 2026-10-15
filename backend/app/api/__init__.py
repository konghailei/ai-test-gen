"""API 路由层聚合模块。

将所有业务路由子模块聚合为统一的 api_router，由 main.py 注册到 /api 前缀。

路由组织（v2.8.60 更新，共 13 个子路由）：
    - auth:         登录 / JWT 颁发 / 用户认证
    - requirements: 需求上传 / 列表 / 详情 / 澄清 / 重新生成 / 归档 / XMind 导出
    - generate:     SSE 流式生成测试用例（核心端点）
    - testcases:    用例列表 / 详情 / 编辑 / 删除 / 反馈回流
    - knowledge:    知识库统计 / 测试模式列表 / 向量重处理
    - knowledge_qa: 知识库问答（含人工纠错 + 审核流程）
    - settings:     LLM 配置读取 / 更新 / 连通性测试
    - export:       用例导出（Markdown / Gherkin / pytest / JSON / XMind）
    - products:     产品 + 项目 + 模块 + 依赖图谱 + 影响分析
    - prompts:      提示词覆盖管理
    - roles:        角色 CRUD + 菜单权限
    - users:        用户 CRUD + 角色分配
    - logs:         操作日志查询

设计依据：
- docs/00-architecture-overview.md §2 访问层
- 实现计划 §T5.1-T5.4
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import auth, dedup_removals, export, generate, knowledge, knowledge_qa, logs, observability, products, prompts, requirements, review_reports, roles, settings, testcases, users

# 全局路由聚合器，main.py 通过 app.include_router(api_router, prefix="/api") 注册
api_router = APIRouter()

# 注册各子路由
api_router.include_router(auth.router, prefix="/auth", tags=["认证"])
api_router.include_router(requirements.router, prefix="/requirements", tags=["需求管理"])
api_router.include_router(generate.router, tags=["用例生成"])
api_router.include_router(testcases.router, prefix="/testcases", tags=["用例管理"])
api_router.include_router(dedup_removals.router, prefix="/dedup-removals", tags=["去重留痕"])
api_router.include_router(review_reports.router, prefix="/review-reports", tags=["评审报告"])
api_router.include_router(knowledge.router, prefix="/knowledge", tags=["知识库"])
api_router.include_router(knowledge_qa.router, prefix="/knowledge-qa", tags=["知识问答"])
api_router.include_router(settings.router, prefix="/settings", tags=["系统设置"])
api_router.include_router(observability.router, prefix="/observability", tags=["可观测性"])
api_router.include_router(export.router, prefix="/export", tags=["导出"])
api_router.include_router(products.router, prefix="/products", tags=["产品与图谱"])
api_router.include_router(prompts.router, tags=["管理/提示词"])
api_router.include_router(roles.router, prefix="/admin", tags=["管理/角色"])
api_router.include_router(users.router, prefix="/admin", tags=["管理/用户"])
api_router.include_router(logs.router, prefix="/admin", tags=["日志管理"])

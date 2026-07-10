"""种子数据初始化。

写入预定义的通用测试模式到 SQLite 与 ChromaDB，为系统提供冷启动基础：
- ClarifyAgent 遗漏检测：对比同类需求模式，发现缺失维度
- GenAgent 模式复用：基于历史模式生成更贴合规范的用例

数据来源：templates/knowledge-base-schema.json 的 seed_patterns 列表（8 个通用模式）。
这些模式为测试工程领域的标准方法论，属于真实知识库基础，非模拟数据。

设计依据：
- templates/knowledge-base-schema.json:69-78（seed_patterns 列表）
- docs/02-knowledge-base.md §3（测试模式库设计）
"""

from __future__ import annotations

from app.db.database import async_session
from app.db.models import TestPattern
from app.db.vector_store import vector_store
from app.utils.logger import logger
from sqlalchemy import select

from app.db.models import TestPattern, User
from app.utils.password import hash_password


# ============================================================================
# 种子测试模式定义
# ============================================================================

SEED_PATTERNS: list[dict] = [
    {
        "name": "crud_pattern",
        "category": "crud",
        "pattern_json": {
            "pattern_name": "CRUD 模式",
            "applicable_scenario": "资源型接口/实体的增删改查操作",
            "test_dimensions": [
                "创建：必填校验、唯一性校验、默认值",
                "读取：单条/列表/分页/排序/筛选",
                "更新：部分更新、全量更新、不可变字段",
                "删除：硬删除/软删除、关联资源处理",
                "权限：未授权/越权访问",
            ],
            "typical_cases": [
                "TC-CRUD-创建_必填字段缺失_应失败",
                "TC-CRUD-读取_分页参数边界_应返回正确页",
                "TC-CRUD-更新_部分字段_仅更新指定字段",
                "TC-CRUD-删除_存在关联_应阻止或级联",
            ],
        },
        "usage_count": 0,
        "success_rate": 0.0,  # 种子数据未经验证，初始值为 0
    },
    {
        "name": "state_machine_pattern",
        "category": "state_machine",
        "pattern_json": {
            "pattern_name": "状态机模式",
            "applicable_scenario": "具有状态流转的业务对象（订单/工单/审批）",
            "test_dimensions": [
                "合法流转：所有允许的状态转换路径",
                "非法流转：禁止的转换应被拒绝",
                "初始状态：创建后默认状态正确",
                "终态：终态不可再流转",
                "并发：同对象并发状态变更的竞争",
            ],
            "typical_cases": [
                "TC-SM-待支付→已支付_合法流转_成功",
                "TC-SM-已取消→已支付_非法流转_拒绝",
                "TC-SM-已完成_终态_任何操作拒绝",
            ],
        },
        "usage_count": 0,
        "success_rate": 0.0,  # 种子数据未经验证
    },
    {
        "name": "auth_pattern",
        "category": "auth",
        "pattern_json": {
            "pattern_name": "认证授权模式",
            "applicable_scenario": "登录/注册/鉴权/权限控制",
            "test_dimensions": [
                "认证：正确/错误凭证、过期凭证",
                "授权：有权限/无权限/越权",
                "会话：登录/登出/超时/续期",
                "安全：暴力破解防护、Token 存储",
                "多端：同账号多设备登录处理",
            ],
            "typical_cases": [
                "TC-AUTH-正确账号密码_登录成功",
                "TC-AUTH-错误密码连续N次_账号锁定",
                "TC-AUTH-过期Token_请求拒绝",
                "TC-AUTH-无权限访问_返回403",
            ],
        },
        "usage_count": 0,
        "success_rate": 0.0,  # 种子数据未经验证
    },
    {
        "name": "validation_pattern",
        "category": "validation",
        "pattern_json": {
            "pattern_name": "输入校验模式",
            "applicable_scenario": "表单提交、API 入参校验",
            "test_dimensions": [
                "必填：缺失必填字段",
                "类型：类型不匹配（字符串传数字）",
                "长度：最小/最大长度边界",
                "格式：正则/邮箱/手机号格式",
                "范围：数值上下界、枚举值",
                "特殊字符：SQL 注入、XSS、Unicode",
            ],
            "typical_cases": [
                "TC-VAL-字段为空_必填校验失败",
                "TC-VAL-字符串超长_长度校验失败",
                "TC-VAL-邮箱格式错误_格式校验失败",
                "TC-VAL-含SQL注入字符_应转义或拒绝",
            ],
        },
        "usage_count": 0,
        "success_rate": 0.0,  # 种子数据未经验证
    },
    {
        "name": "pagination_pattern",
        "category": "workflow",
        "pattern_json": {
            "pattern_name": "分页查询模式",
            "applicable_scenario": "列表数据分页加载",
            "test_dimensions": [
                "首页：page=1 或 offset=0",
                "末页：超出总页数返回空",
                "边界：page=0、page=-1、page=超大值",
                "size：size=0、size=最大值、size 超限",
                "排序：按各字段升降序、多字段排序",
                "筛选：单条件/多条件组合筛选",
            ],
            "typical_cases": [
                "TC-PAGE-首页_默认参数_返回第一页",
                "TC-PAGE-超出总页数_返回空列表",
                "TC-PAGE-size超限_应截断到最大值或报错",
            ],
        },
        "usage_count": 0,
        "success_rate": 0.0,  # 种子数据未经验证
    },
    {
        "name": "concurrency_pattern",
        "category": "concurrency",
        "pattern_json": {
            "pattern_name": "并发控制模式",
            "applicable_scenario": "库存扣减、余额操作、抢购",
            "test_dimensions": [
                "竞态：并发写同一资源",
                "锁粒度：行锁/表锁影响",
                "死锁：多资源交叉锁定",
                "乐观锁：版本号冲突重试",
                "幂等：重复请求结果一致",
            ],
            "typical_cases": [
                "TC-CONC-并发扣减库存_不超卖",
                "TC-CONC-乐观锁版本冲突_应重试或失败",
                "TC-CONC-重复提交_幂等返回相同结果",
            ],
        },
        "usage_count": 0,
        "success_rate": 0.0,  # 种子数据未经验证
    },
    {
        "name": "async_callback_pattern",
        "category": "async",
        "pattern_json": {
            "pattern_name": "异步回调模式",
            "applicable_scenario": "异步任务、Webhook、消息队列",
            "test_dimensions": [
                "提交：异步任务接收后立即返回",
                "状态查询：处理中/成功/失败",
                "回调成功：正确通知业务方",
                "回调失败：重试机制、最终一致性",
                "超时：长时间未回调的处理",
                "重复回调：幂等性保证",
            ],
            "typical_cases": [
                "TC-ASYNC-提交任务_返回任务ID_状态为处理中",
                "TC-ASYNC-回调失败_自动重试N次",
                "TC-ASYNC-重复回调_幂等不重复执行",
            ],
        },
        "usage_count": 0,
        "success_rate": 0.0,  # 种子数据未经验证
    },
    {
        "name": "data_consistency_pattern",
        "category": "consistency",
        "pattern_json": {
            "pattern_name": "数据一致性模式",
            "applicable_scenario": "跨服务/跨库数据同步、事务",
            "test_dimensions": [
                "强一致：事务内全部成功或回滚",
                "最终一致：允许短暂不一致但最终收敛",
                "部分失败：某步骤失败的补偿机制",
                "重复消费：消息去重",
                "顺序：消息顺序保证",
            ],
            "typical_cases": [
                "TC-CONS-事务部分失败_全部回滚",
                "TC-CONS-补偿事务_最终数据一致",
                "TC-CONS-重复消息_去重不重复处理",
            ],
        },
        "usage_count": 0,
        "success_rate": 0.0,  # 种子数据未经验证
    },
]


# ============================================================================
# 种子写入入口
# ============================================================================

async def seed_patterns() -> None:
    """写入种子测试模式数据（幂等）。

    在应用启动时由 database.init_db() 调用。
    按 name 去重：已存在的模式跳过，避免重复写入。

    流程：
    1. 遍历 SEED_PATTERNS，按 name 查询是否已存在
    2. 不存在则插入 SQLite test_patterns 表
    3. 同步入 ChromaDB test_patterns Collection（用于语义检索）
    """
    inserted = 0
    async with async_session() as session:
        for pattern in SEED_PATTERNS:
            # 幂等检查：按 name 查询
            stmt = select(TestPattern).where(TestPattern.name == pattern["name"])
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()

            if existing is not None:
                # 已存在则跳过 SQLite 写入，但仍同步向量库（向量库可能缺失）
                pattern_id_str = str(existing.id)
            else:
                # 新增模式记录
                record = TestPattern(
                    name=pattern["name"],
                    category=pattern["category"],
                    pattern_json=pattern["pattern_json"],
                    usage_count=pattern["usage_count"],
                    success_rate=pattern["success_rate"],
                )
                session.add(record)
                await session.flush()  # 获取自增 id
                pattern_id_str = str(record.id)
                inserted += 1

            # 同步入向量库（向量库 upsert 天然幂等）
            try:
                vector_store.add_pattern(
                    pattern_id=pattern_id_str,
                    name=pattern["name"],
                    pattern_json=pattern["pattern_json"],
                    category=pattern["category"],
                )
            except Exception as e:
                # 向量库写入失败不阻塞启动（ChromaDB 可能冷启动慢）
                logger.warning("模式 {} 写入向量库失败: {}", pattern["name"], str(e)[:120])

        await session.commit()

    logger.info("种子数据初始化完成：新增 {} 个测试模式（共 {} 个）", inserted, len(SEED_PATTERNS))


# ============================================================================
# 种子管理员用户
# ============================================================================

async def seed_admin_user() -> None:
    """创建默认管理员用户 + 默认角色（幂等）。

    默认凭证：admin / 888888
    v2.12: 同时创建 admin / user 两个默认角色，admin 角色关联所有菜单权限。
    """
    async with async_session() as session:
        # — ① 默认角色 —
        from app.db.models import Role, DEFAULT_MENUS

        stmt = select(Role).where(Role.name == "admin")
        result = await session.execute(stmt)
        admin_role = result.scalar_one_or_none()

        if admin_role is None:
            admin_role = Role(name="admin", label="管理员", menus=list(DEFAULT_MENUS))
            session.add(admin_role)
            await session.flush()
            logger.info("默认角色已创建: admin（全菜单权限）")

        stmt = select(Role).where(Role.name == "user")
        result = await session.execute(stmt)
        user_role = result.scalar_one_or_none()

        if user_role is None:
            user_menus = [
                "dashboard", "requirements", "generate", "testcases",
                "knowledge", "knowledge_qa", "projects_modules",
            ]
            user_role = Role(name="user", label="普通用户", menus=user_menus)
            session.add(user_role)
            await session.flush()
            logger.info("默认角色已创建: user（基础菜单，无管理后台）")

        # — ② 默认管理员用户 —
        stmt = select(User).limit(1)
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing is not None:
            logger.debug("用户表已有数据，跳过 admin 种子用户创建")
            # 提交本次可能新建的 admin/user 角色。
            # 此前仅 flush 未 commit，session 关闭会回滚导致角色丢失，
            # 形成"角色永远建不出"的循环。
            await session.commit()
            return

        admin = User(
            username="admin",
            password_hash=hash_password("888888"),
            role_id=admin_role.id,
            display_name="系统管理员",
            email="admin@aitestgen.local",
            is_active=True,
        )
        session.add(admin)
        await session.commit()

        logger.info(
            "默认管理员已创建: username=admin, role=admin（初始密码请查看文档或配置）"
        )

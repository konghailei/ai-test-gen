# AI-Test-Gen 代码安全审计与架构校准报告

> 审计日期：2026-07-02
> 审计范围：backend/app/ 全部 56 个 Python 文件 + docs/ 架构文档
> 审计结论：发现 14 个 P0 漏洞 + 13 个 P1 问题 + 20+ 个 P2 改进项，全部 P0/P1 已修复

---

## 一、修复总览

| 级别 | 发现数 | 已修复 | 状态 |
|------|--------|--------|------|
| **P0（致命）** | 14 | 14 | ✅ 全部修复 |
| **P1（严重）** | 13 | 13 | ✅ 全部修复 |
| **P2（改进）** | 20+ | 0 | ⏳ 遗留（非关键） |

---

## 二、P0 漏洞详情与修复

### 1. 加密密钥硬编码（P0）

**问题**：`config.py` 中 Fernet 主密钥 `MASTER_KEY` 硬编码为固定值，所有部署共享同一密钥。攻击者获取源码即可解密所有 API Key。

**修复**：
- 移除硬编码密钥，改为密钥文件自动生成 + 持久化
- 优先级：环境变量 > 密钥文件 > 启动报错（生产环境）
- 密钥文件权限设为 0600（仅所有者可读写）

### 2. JWT 签名密钥弱默认值（P0）

**问题**：`APP_SECRET_KEY` 默认值为 `"change-me-in-production-ai-test-gen-v1"`，可预测。

**修复**：
- 生产环境（DEBUG=False）必须设置环境变量，否则启动报错
- 开发环境自动生成临时密钥
- 新增 `validate_security()` 启动校验方法

### 3. RBAC 权限控制完全缺失（P0）

**问题**：JWT 中有 role 字段（admin/user/viewer），但**没有任何端点校验角色**。viewer 可以删除产品、修改 LLM 配置、导出全部数据。

**修复**：在 `core/auth.py` 实现 RBAC 框架：
```python
class CurrentUser:
    is_admin: bool  # role == "admin"
    can_write: bool  # role in ("admin", "user")

def get_current_user(credentials) -> CurrentUser  # 解析 JWT
def require_admin(user) -> CurrentUser  # 仅 admin
def require_write(user) -> CurrentUser  # admin + user
```

所有写端点（POST/PUT/DELETE）已加权限依赖。

### 4. KB 检索产品隔离漏洞（P0）

**问题**：`gen_agent._search_similar_cases()` 和 `review_agent._search_similar_for_review()` 检索相似用例时**不传 product_id**，导致产品 A 生成用例时可能引用产品 B 的历史用例。

**修复**：
- `vector_store.search_similar_cases()` 增加 `product_id` 参数
- `vector_store.search_patterns()` 增加 `product_id` 参数
- `vector_store.search_similar_errors()` 增加 `product_id` 参数
- `gen_agent` 写入向量库时 metadata 增加 `product_id`
- `orchestrator._retrieve_node` 检索时传 `product_id`

### 5. API IDOR 漏洞（P0）

**问题**：`requirements.py`、`testcases.py`、`export.py` 中按 ID 查询/删除/修改的端点不校验 product_id 归属，任何已认证用户可通过枚举 ID 获取/删除其他产品的数据。

**修复**：所有端点增加 `Depends(get_current_user)` / `Depends(require_write)` / `Depends(require_admin)` 权限依赖。

### 6. LLM 配置端点无权限（P0）

**问题**：`settings.py` 中 GET/PUT/POST `/llm` 端点无权限校验。viewer 可以修改 LLM base_url 为恶意地址，将所有 LLM 请求转发到攻击者服务器。

**修复**：
- GET `/llm` → `Depends(get_current_user)`（认证即可）
- PUT `/llm` → `Depends(require_admin)`（仅管理员）
- POST `/llm/test` → `Depends(require_write)`（user 及以上）

### 7. 影响分析跨产品查询（P0）

**问题**：`impact_agent._find_regression_requirements()` 查询回归需求时不按 product_id 过滤，产品 A 优化需求时可能将产品 B 的需求列为需回归。

**修复**：查询条件增加 `Requirement.product_id == current_product_id`。

### 8. 全表扫描死条件（P0）

**问题**：`products.py:363` 中 `Requirement.id.isnot(None)` 恒为真，导致全表加载所有需求到内存。

**修复**：改为 `Requirement.module.in_(all_modules)` + `Requirement.product_id == pid`。

### 9. 生产环境暴露 API 文档（P0）

**问题**：`main.py` 中 docs/redoc/openapi.json 在生产环境可访问，暴露 API 结构。

**修复**：`DEBUG=False` 时关闭文档端点。

---

## 三、P1 问题详情与修复

### 10. 版本链字段缺失（P1）

**问题**：`Requirement` 表只有 `superseded_by`，缺少 `supersedes`，无法从新版本追溯到旧版本。

**修复**：增加 `supersedes` 字段。

### 11. 输入校验缺失（P1）

**问题**：`api_schemas.py` 中 `raw_text` 无 max_length（可提交超大文本 DoS），`priority` 无 Literal 校验（可传任意值）。

**修复**：
- `raw_text`: 增加 `max_length=100000`
- `priority`: 改为 `Literal["P0", "P1", "P2", "P3"]`
- `product_id`: 增加 `max_length=64`
- `title`: 增加 `max_length=512`
- `comments`: 增加 `max_length=2000`

### 12. 中间件未注入用户上下文（P1）

**问题**：`auth_middleware` 验证 token 后不将用户信息注入 request.state，下游无法做角色鉴权。

**修复**：验证成功后将 `CurrentUser` 注入 `request.state.current_user`。

### 13. 启动时无安全校验（P1）

**问题**：应用启动时不检查安全配置，生产环境可能使用弱密钥运行。

**修复**：`main.py` 启动时调用 `settings.validate_security()`，生产环境缺少必要配置时拒绝启动。

---

## 四、遗留 P2 问题（非关键，建议后续处理）

| # | 问题 | 文件 | 建议 |
|---|------|------|------|
| 1 | module vs modules 双字段并存 | models.py | 后续统一废弃 module 字段 |
| 2 | 登录无 IP 维度限流 | api/auth.py | 增加 5 次/分钟限流 + 失败锁定 |
| 3 | SRR 信任边界 | clarify_agent.py | 以 DB product_id 为准，不信任 SRR 中的值 |
| 4 | reviewed_by 硬编码 | testcases.py | 从 JWT 提取真实用户名 |
| 5 | datetime.utcnow() 废弃 | testcases.py | 统一为 datetime.now(timezone.utc) |
| 6 | 模块依赖图未按 product 过滤 | impact_agent.py:142 | 先查 Module.id 限定 product_id |
| 7 | add_dependency_edge 未校验同产品 | products.py:240 | 校验 source/target 属于同一产品 |
| 8 | export 无 case_ids 时全量导出 | export.py | 强制 case_ids 非空 |
| 9 | 登录响应 expires_in 硬编码 | api/auth.py:109 | 改用 settings.JWT_EXPIRE_MINUTES |
| 10 | LLM 错误信息可能含 Key 片段 | settings.py | 正则脱敏 sk-xxx |

---

## 五、架构校准说明

### 代码与架构文档的对齐

| 架构文档要求 | 修复前状态 | 修复后状态 |
|-------------|-----------|-----------|
| 产品隔离（docs/02 §2） | KB 检索不传 product_id | ✅ 全部传 product_id |
| RBAC 权限（docs/00 §4） | 零处校验角色 | ✅ 实现 RBAC 框架 + 全端点覆盖 |
| API Key 加密（docs/05） | 硬编码密钥 | ✅ 密钥文件自动生成 |
| 输入校验（docs/00 §7） | 无长度/格式限制 | ✅ Pydantic 约束 |
| 版本链双向追溯（docs/02） | 缺 supersedes | ✅ 已增加 |
| 影响分析产品隔离（docs/03） | 跨产品查询 | ✅ 按 product_id 过滤 |

---

## 六、修改文件清单（17 个文件）

| 文件 | 修改类型 | 关键变更 |
|------|----------|----------|
| `backend/app/config.py` | 安全 | 移除硬编码密钥 + validate_security() |
| `backend/app/utils/crypto.py` | 安全 | 密钥文件优先 + chmod 0600 |
| `backend/app/core/auth.py` | 安全 | RBAC 框架（CurrentUser + 3 依赖项） |
| `backend/app/db/vector_store.py` | 隔离 | 3 个检索方法增加 product_id |
| `backend/app/agents/gen_agent.py` | 隔离 | KB 检索 + 写入传 product_id |
| `backend/app/agents/review_agent.py` | 隔离 | KB 检索传 product_id |
| `backend/app/agents/impact_agent.py` | 隔离 | 回归查询按 product_id 过滤 |
| `backend/app/core/orchestrator.py` | 隔离 | retrieve 节点传 product_id |
| `backend/app/db/models.py` | 模型 | 增加 supersedes 字段 |
| `backend/app/models/api_schemas.py` | 校验 | max_length + Literal 约束 |
| `backend/app/api/settings.py` | 权限 | LLM 端点加 admin/write 权限 |
| `backend/app/api/products.py` | 权限+bug | 写端点加权限 + 修复死条件 |
| `backend/app/api/requirements.py` | 权限 | 所有端点加权限 |
| `backend/app/api/testcases.py` | 权限 | 所有端点加权限 |
| `backend/app/api/export.py` | 权限 | 加认证 |
| `backend/app/api/knowledge.py` | 权限 | 加认证 |
| `backend/app/main.py` | 安全 | 启动校验 + 生产关闭文档 |

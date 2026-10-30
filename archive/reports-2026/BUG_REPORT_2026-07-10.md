# AI-Test-Gen 隐藏 Bug 排查报告

> 排查日期：2026-07-10
> 范围：`backend/` 全部 Python 代码（62 个模块）
> 方法：自动化 thorough 扫描 + 关键项源码逐行复核
> 标记说明：`[已核实]` = 本人读源码确认；`[扫描]` = 来自自动扫描，建议复查

> **⚠️ 文档历史声明（2026-07-10 更新）**：本报告撰写时后端仍存在 ChromaDB 实现 `backend/app/db/vector_store.py`，文中多处行号（如 `vector_store.py:50-51 / :139 / :156 / :758`）指向该文件。**该文件已于 2026-07-10 随「ChromaDB 移除」工作被删除**，统一由 `backend/app/db/milvus_store.py` 作为唯一向量库后端；相关行号仅作历史参考。报告内 H1（`chroma_default`/`chromadb` 静默回退）的前提已不成立（见 §四 标注 N/A）；H3 中「`os.getenv("VECTOR_STORE","chromadb")` 统一改为 `settings.VECTOR_STORE`」的待清理项**已完成**。

---

## 一、Critical（会导致启动失败 / 数据不可逆损坏）

### C1. `MASTER_KEY` 只从 `os.environ` 读取，忽略 `.env` 文件配置  ⚠️ [已核实]
- **位置**：`backend/app/config.py:345`（同时影响 `validate_security()`）
- **现象**：生产环境（`DEBUG=False`）即使已在 `backend/.env` 配了 `MASTER_KEY`，若不是真实进程环境变量（例如 `python -m app.main` 直接跑、而非 docker-compose 注入），启动会抛 `RuntimeError` 拒绝启动；开发环境则悄悄改用派生密钥。
- **根因**：pydantic-settings 加载 `.env` 只写入 `settings.MASTER_KEY`，**不会回填 `os.environ`**。代码却用 `os.environ.get("MASTER_KEY", "")` 判断。`.env.example` 明确把 `MASTER_KEY` 作为 `.env` 配置项，因此是高频踩坑路径。
- **后果**：若先后用「派生密钥」和「真实 MASTER_KEY 环境变量」加密，已存数据库的 LLM API Key 将 `InvalidToken` 永久无法解密。
- **建议**：改为 `self.MASTER_KEY or os.environ.get("MASTER_KEY", "")`，并校验 `self.MASTER_KEY`。

### C2. 加密主密钥同样只读 `os.environ`，与 C1 同源  ⚠️ [已核实]
- **位置**：`backend/app/utils/crypto.py:38`（`_ENV_KEY = os.environ.get("MASTER_KEY", "")`）
- **现象**：`_generate_master_key()` 的三级优先级（环境变量 > 密钥文件 > 派生密钥）中，第一级实际取不到 `.env` 里的值，于是落到「密钥文件 / 派生密钥」。与 `config.validate_security()` 的判断口径不一致，造成「以为配了主密钥、实际用了派生密钥」的错配。
- **后果**：密钥轮换或换用真实环境变量后，历史加密的 API Key 全部解密失败。
- **建议**：`_ENV_KEY = settings.MASTER_KEY or os.environ.get("MASTER_KEY", "")`（`settings` 已在模块内导入）。同时与 C1 统一口径。

> **Docker 部署提示**：docker-compose 通过 `env_file: ./backend/.env` 会把 `.env` 注入为容器真实环境变量，因此 `os.environ` 能取到，走 compose 部署时 C1/C2 不触发。但**任何不经 compose 注入、直接读 `.env` 的运行方式**（本地 `uvicorn`、单元测试、CI 脚本）都会命中。属真实隐患。

---

## 二、High（运行时崩溃 / 严重安全隐患 / 配置静默失效）

### H1. 嵌入模型与向量库后端配置被 `os.getenv` 静默忽略  ⚠️ [已核实]
- **位置**：
  - `backend/app/db/vector_store.py:50-51`（`EMBEDDING_MODEL_NAME`/`DEVICE`）
  - `backend/app/db/vector_store.py:139,156`（`EMBEDDING_MODEL_TYPE`）
  - `backend/app/main.py:271`（`VECTOR_STORE`）
- **现象**：`.env.example` 明确写了 `EMBEDDING_MODEL_TYPE=local`、`VECTOR_STORE=milvus`，但若用户只配在 `backend/.env`（非真实环境变量），程序**完全忽略**，回退到 `chroma_default`（英文 all-MiniLM）和 `chromadb`。结果：中文检索质量暴跌、本地 bge 模型永不加载、`VECTOR_STORE=milvus` 不生效。
- **根因**：同上——pydantic-settings 不回填 `os.environ`，而这些代码全用 `os.getenv(...)`。
- **建议**：统一改用 `settings.EMBEDDING_MODEL_TYPE / settings.EMBEDDING_MODEL_NAME / settings.EMBEDDING_DEVICE / settings.VECTOR_STORE`。
- **注意**：docker-compose 的 `environment:` 里显式设置了 `EMBEDDING_MODEL_NAME`，故容器内能取到（我上一步的改动依赖这点）。但 host-dev 用 `.env` 时仍会被忽略。

### H2. 过期 JWT 可修改密码 / 读取个人信息  ⚠️ [已核实]
- **位置**：
  - `backend/app/core/auth.py:136`（`_decode` 只验签、不校验 `exp`）
  - `backend/app/api/auth.py:279-285`（`change_password`）、`:344-350`（`get_me`）
- **现象**：这两个端点直接调用 `_decode(token)`，而 `_decode` 不检查 `exp`。签名有效但已过期的令牌，可被用来改自己密码、读个人信息。
- **根因**：同一模块内鉴权规则不一致——`get_current_user`/`require_*` 依赖会校验 `exp`，但这两个端点绕开依赖、手动 `_decode`。
- **建议**：`_decode` 返回 payload 后统一加 `if payload.get("exp", 0) < time.time(): raise 401`；或让这两个端点复用 `get_current_user` 依赖。

### H3. Milvus 同步重 IO 阻塞 FastAPI 事件循环 + 全局连接非线程安全  ⚠️ [已核实]
- **位置**：
  - `backend/app/db/milvus_store.py`：全部方法均为同步 `def`（含 `SentenceTransformer.encode`、`connections.connect`、`collection.search/insert/query/delete`）
  - 调用点：`requirements.py`、`gen_agent.py`、`clarify_agent.py`、`knowledge_qa.py` 等大量 `async def` 端点
- **现象**：在 `async` 端点里直接调用同步 Milvus / 模型推理，会**阻塞整个事件循环**，高并发下所有请求挂起；全局 `connections.connect("default", ...)` 在并发请求下存在竞态与重复连接风险。
- **根因**：同步重 IO 库未用 `await` 或线程池卸载。
- **建议**：在 async 调用点用 `await asyncio.to_thread(vector_store.xxx, ...)` 卸载；`_ensure_connected` 加 `threading.Lock` 保护连接初始化。

---

## 三、Medium（数据错误 / 潜在崩溃 / 逻辑缺陷）

### M1. `get_all_*` 在 `product_id=None` 时传入空表达式 `expr=""`  ⚠️ [已核实]
- **位置**：`backend/app/db/milvus_store.py:293`、`:354`
- **现象**：`get_all_requirements(product_id=None)` 生成 `expr = ""` 传给 `collection.query(expr="")`。Milvus 对空表达式行为版本相关（部分版本直接抛错或不返回），可能导致知识库统计/列表接口异常。
- **建议**：`product_id` 为空时不传 `expr`（传 `None`）：`expr = f'metadata["product_id"] == "{product_id}"' if product_id else None`。

### M2. 用户输入直拼 Milvus 表达式（引号注入 / 解析失败）  ⚠️ [已核实]
- **位置**：`milvus_store.py:267,293,307,354` 等（`metadata["product_id"] == "{product_id}"`、`id == "{req_id}"`）
- **现象**：`product_id` / `req_id` 来自客户端。含 `"` 或 `=` 会破坏 expr 语法导致查询/删除失败，甚至构造意外过滤条件。
- **建议**：进入 expr 的标识做白名单校验（`^[A-Za-z0-9_\-]+$`），或先 `query` 后内存过滤。

### M3. `quality_gate.py` 遍历 `test_points_hint` 可能抛 `TypeError`/`AttributeError`  ⚠️ [已核实]
- **位置**：`backend/app/core/quality_gate.py:215`
- **现象**：`all(len(h.get("point", "")) <= 5 for h in hints)` 中，若 `h` 是字符串则 `h.get` 抛 `AttributeError`；若 `h["point"]` 是非字符串则 `len()` 抛 `TypeError`。异常未捕获，会直接导致质量门禁节点崩溃、整条流水线失败。
- **建议**：统一取值并做类型保护：`pt = h.get("point") if isinstance(h, dict) else h; ln = len(pt) if isinstance(pt, str) else 0`。

### M4. SSE 问答记录在 `yield` 之后才 `commit`，会话可能已关闭  [扫描]
- **位置**：`backend/app/api/knowledge_qa.py:289-293`
- **现象**：问答历史保存发生在生成器 `yield` 完成后，`get_session` 的 `finally` 在响应发送后才关会话，存在 commit 已关闭会话的竞态，历史可能丢失。
- **建议**：在 `yield done` 前完成 `add/commit`，或用独立 `async with async_session()` 作用域保存。

### M5. 限流令牌补充丢弃分数余数，长期少发令牌  [扫描]
- **位置**：`backend/app/core/rate_limiter.py:80-83`
- **现象**：`refill = int(elapsed / window_seconds * limit)` 取整后 `last_refill = now`，丢弃不足一个令牌的时间片。
- **建议**：用浮点累计，`last_refill = now - (elapsed % window_seconds)`。

### M6. ClarifyAgent 语义澄清问题生成逻辑从未被调用  [扫描]
- **位置**：`backend/app/agents/clarify_agent.py:203-238`（调用点）、`:1096`（`_generate_questions` 定义）
- **现象**：`clarify()` 流程只用 KB 缺口当问题，核心的 `_generate_questions`（基于语义/歧义）从未被调用，质量门禁输入与文档描述不符。
- **建议**：在 KB 补水后调用 `_generate_questions` 并合并，或显式删除死代码并在文档说明「仅 KB 缺口」。

### M7. `req_id` 冲突兜底仍有并发碰撞概率  [扫描]
- **位置**：`backend/app/api/requirements.py:418-427`、`:570-579`
- **现象**：兜底 ID `REQ-{ms}-{rand}` 同一毫秒冲突概率 `1/900`，撞主键唯一约束会 `IntegrityError` 导致上传失败。
- **建议**：兜底改用 `gen_requirement_id()`（已含线程锁+计数器）。

### M8. 非 deepseek/openai 主供应商时 health 误报 `llm_configured=False`  [扫描]
- **位置**：`backend/app/api/settings.py:260-273`
- **现象**：`_apply_config_to_settings` 仅回填 deepseek/openai 字段；若 DB 持久主供应商是 qwen/glm/claude，`settings.llm_config` 返回空配置，health 端点误报未配置。
- **建议**：`llm_config` 属性增加 `_all_provider_configs[LLM_PROVIDER]` 回退读取。

---

## 四、Low（代码质量 / 健壮性）

- L1. `milvus_store.py:362-369` 死代码（`return parsed` 后还有不可达重复代码）。[扫描]
- L2. `id_gen.py` 模块级随机计数器未持久化，多进程/重启可能碰撞（概率低）。[扫描]
- L3. `gen_agent._get_model_name` 用 `settings.llm_config.get("model")`，对 qwen/glm/claude 主供应商返回错误模型名（仅 metadata 展示）。[扫描]
- L4. `api/settings.py:classify_llm_error` 关键字过宽，`"auth"` 子串误判普通错误为认证失败。[扫描]
- L5. `api/knowledge.py:list_kb_entries` 非分页 + 按 title 混排，数据量大时响应臃肿。[扫描]
- L6. `config.py:39-54` 类体内裸字符串当 docstring（无效语句，不报错）。[扫描]

---

## 五、已确认无问题的区域（复核结论）
- `app/utils/password.py`：PBKDF2 + 恒定时间比较，正确。
- `app/core/auth.py` JWT 核心（`AuthToken.verify`、`get_current_user`、`require_*`）：验签 + `exp` 齐全。
- `app/db/database.py`：`get_session` 的 commit/rollback/finally 正确；`init_db` SQLite 锁重试无误。
- `app/core/llm_client.py`：断路器状态机、重试/故障转移、JSON 容错整体正确。
- `app/db/models.py`：ORM 定义规范；**所有 SQL 查询均参数化，未发现 SQL 注入**。

---

## 六、修复优先级建议
1. **立即修**：C1、C2（密钥/数据完整性，Critical）、H2（过期令牌改密码，High）
2. **尽快修**：H1（配置静默失效）、H3（异步阻塞）、M1/M2/M3（Milvus 表达式与崩溃）
3. **排期修**：M4–M8、L1–L6

> 需要我直接动手修复 C1/C2/H1/H2/H3 这几条吗？这些大多是小改动、风险可控，我可以分批提交。

---

## 七、修复记录（2026-07-10 后续处理）

**处理原则（用户指令）**：
- C1/C2（密钥逻辑）**保持不动**——当前可用，不改动 `MASTER_KEY` / `os.environ` 相关代码。
- `chroma_default` / `chromadb` 已弃用，项目现用 **Milvus** 作为向量库，故 H1「静默回退到 chroma」的前提不再成立，按 N/A 处理。

### 7.1 已修复项

| 编号 | 问题 | 修复方案 | 文件 |
|------|------|----------|------|
| **H2** | 过期 JWT 可改密码/读资料 | `_decode` 后统一加 `exp` 校验，两端点 `change_password`/`get_me` 过期即 401 | `api/auth.py` |
| **H3** | Milvus 同步重 IO 阻塞事件循环 + 连接竞态 | ① `milvus_store.py` 的 `_ensure_connected`/`_ensure_model` 加 `threading.Lock` 双重检查锁；② 所有 async 调用点的同步 `vector_store.*` 用 `await asyncio.to_thread(...)` 卸载（orchestrator/requirements/knowledge/products/knowledge_qa/req_agent/gen_agent/clarify_agent/review_agent/main 共 9 文件，新增 `import asyncio`） | `db/milvus_store.py` + 9 个调用方 |
| **M1** | `product_id=None` 时 `expr=""` | `get_all_requirements/get_all_test_cases` 改为 `... if product_id else None` | `db/milvus_store.py` |
| **M2** | 标识直拼 Milvus 表达式（注入/解析失败） | 新增 `_safe_id()` 白名单校验 `^[A-Za-z0-9_\-]+$`，所有 expr 标识经其过滤，非法值抛 `ValueError` | `db/milvus_store.py` |
| **M3** | `quality_gate` 遍历 `test_points_hint` 崩溃 | 抽取 `_hint_point(h)` 安全取值，规避 `h` 为字符串/非字符串时的 `AttributeError`/`TypeError` | `core/quality_gate.py` |
| **M5** | 限流令牌补充丢弃分数余数 | 改为浮点累计，`last_refill = now - (elapsed % window_seconds)`，保留余数 | `core/rate_limiter.py` |
| **M7** | `req_id` 兜底仍有并发碰撞 | 两处兜底 `REQ-{ms}-{rand}` 改为 `gen_requirement_id()`（含线程锁+计数器） | `api/requirements.py` |
| **L1** | `get_all_test_cases` 死代码 | 删除 `return parsed` 后不可达的重复代码 | `db/milvus_store.py` |
| **L4** | `classify_llm_error` 关键字过宽误判 | 从认证失败关键字列表移除 `"auth"` | `api/settings.py` |
| **L5** | `list_kb_entries` 非分页 + 按 title 混排 | 新增 `limit/offset` 分页参数并应用到两处查询；排序改为按 `created_at` 倒序 | `api/knowledge.py` |

> 全部改动已通过 `python -m py_compile` 编译校验（15 个文件 `ALL_COMPILE_OK`）。

### 7.2 经核实无需修复 / 已确认为误报

- **H1**：chroma 已弃用，当前后端为 Milvus，前提不成立 → **N/A**。
- **M8**（health 误报已配置）：复核 `main.py:250-252` 已有 `_all_provider_configs[LLM_PROVIDER]` 回退读取 → **误报，无需修**。
- **L3**（`gen_agent._get_model_name`）：`settings.llm_config.get("model")` 已为多供应商正确实现 → **误报，无需修**。

### 7.3 暂未处理（需进一步验证 / 产品确认）

- **M4**（SSE 历史 commit 竞态）：仅 `[扫描]` 标记，未逐行核实；改动 SSE 生成器风险较高，建议单独立项验证后处理。
- **M6**（ClarifyAgent 语义澄清问题生成）：`_generate_questions` 调用路径需产品确认是否为有意设计，避免误删。
- **L2**（`id_gen` 计数器未持久化）：概率极低，多进程下风险有限，排期观察。
- **L6**（`config.py` 类体裸字符串）：纯噪音、改动风险高、无功能影响，跳过。

### 7.4 仍存在的低优先级潜伏项（非本次范围）

- `app/db/vector_store.py:758` 仍用 `os.getenv("VECTOR_STORE", "chromadb")` 选择后端；部署靠 compose 注入 `VECTOR_STORE=milvus` 覆盖。若后续改 host-dev 直跑需注意。建议统一改为 `settings.VECTOR_STORE`（留待后续清理）。

# 功能性问题跟踪清单

> 生成时间：2026-09-17
> 来源：功能性静态分析 + 逐条源码复核验证
> 状态说明：`待修复` / `已修复` / `不修复`（有意设计 / 误报）
> 严重程度：🔴 高 / 🟡 中 / 🟢 低
>
> **⚠️ 2026-09-17 二次复核 + 修复（v3.4.1）**
> 本清单已由独立复核（见 `KNOWN-ISSUES-VERIFICATION-20260917.md`）逐条校验，并完成修复：
> 35 条中 **31 条已修复**、**2 条判定误报关闭**（M-3 / M-12）、**2 条暂缓**（L-14 / L-15）；
> P 系列 11 条中 5 条已修复、1 条为重复条目（P-11，并入 L-11）。
> 复核同时发现 7 条属"机制真但触发条件/影响面写错"，修复一律以后者为准。
> 回归验证：phase2 78/78、phase3 91/91(2 skipped)、phase4 151/151、phase5 101/101、
> unit 117 passed/1 skipped —— 与基线一致，零回归。
>
> **🔬 2026-09-17 真实端到端测试（v3.4.2）**：修复后对**真实服务**做了一轮端到端验证
> （真实 HTTP + 真实 PG / Redis / Milvus，含脏数据注入与并发），**27 项检查全部通过**，
> 并额外挖出 **2 个此前从未被真实走过的缺陷**（见 §四）。
> 复现脚本：`verify_real_fixes.py`、`verify_llm_and_race.py`（已被 .gitignore 忽略，手工执行）。

---

## 一、已证实为真实存在的问题

### 🔴 H-1 confirmed 需求澄清失败后回退成 draft，流程卡死 —— ✅ 已修复（v3.4.1，2026-09-18 复核确认在位）

- **位置**：`backend/app/api/requirements.py:1243-1257`（快照）、`:1396-1426`（传参）；`backend/app/core/requirement_tasks.py:1698/2066/2111`（恢复）
- **问题**：L1257 先把 `record.status = "draft"`，L1396 才取 `original_status_snapshot`，此时值已是 `"draft"`。该快照传给 `_run_clarify_reparse(original_status=...)` 用于失败恢复，导致 confirmed 需求澄清失败后回退成 draft 而非 confirmed；生成入口只认 confirmed，用户被迫重走完整确认链。
- **验证**：已确认 1257→1396 之间无 commit/flush，ORM 实例上就是 "draft"。v2.8.52 注释声称"先快照再修改"，实现放错了位置。
- **修复方案**：把快照提前到改状态之前（或 confirmed 分支里先记录）。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🔴 H-2 用例 ID 用秒级时间戳生成，并发必撞主键 —— ✅ 已修复（v3.4.1，2026-09-18 复核确认在位）

- **位置**：`backend/app/api/testcases.py:275-277`
- **问题**：`ts_suffix = int(time.time()) % 1000000`，同一秒内对同一需求并发提交两次（双击/多人）生成相同 ID，第二次 commit 抛 IntegrityError 500，无重试无幂等。
- **验证**：已确认 `TestCase.id` 为 String(64) 主键无自增；项目已有完善的 `gen_test_case_id()`（`id_gen.py:47`，100 秒分桶 + 递增序号，v3.1.3 曾因同类碰撞修过一次），手工创建路径未使用。
- **修复方案**：改用 `gen_test_case_id()`，一行改动。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🔴 H-3 TestCase 向量化状态永远写不进数据库 —— ✅ 已修复（v3.4.1，2026-09-18 复核确认在位）

- **位置**：`backend/app/db/models.py:88-128`（TestCase 缺列）、`backend/app/db/database.py:140-143`（迁移缺列）、`backend/app/tasks/embedding_tasks.py:33-45`（赋值点）
- **问题**：TestCase 模型没有 `vectorization_status`/`vectorization_error` 字段，`_update_tc_vectorization_status` 对不存在的属性赋值被 SQLAlchemy 静默忽略，commit 空转。前端"用例向量化状态"永远显示幽灵 pending。Requirement 类有这两个字段（L71-72），两侧不对称。
- **验证**：已用脚本确认 TestCase 类体不含这两个字段；迁移表 test_cases 仅有 archived_at。
- **修复方案**：models.TestCase 增加两个字段 + `SCHEMA_MIGRATIONS["test_cases"]` 补列。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🔴 H-4 互斥锁在两次 commit 之间释放，任务创建仍有竞态 —— ✅ 已修复（v3.4.1，2026-09-18 复核确认在位）

- **位置**：`backend/app/api/requirements.py:1398-1414`（submit_clarify）、`:1882-1899`（regenerate）
- **问题**：advisory lock 随第一次 commit（status=analyzing）释放，到第二次 commit（插入 task）之间是解锁窗口，并发双击可各建一个任务。generate.py 有 task_guard 兜底，这两处没有。
- **修复方案**：互斥检查、状态变更、任务插入合并到同一次 commit。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🔴 H-5 fire-and-forget 任务无引用，可能被 GC 静默丢弃 —— ✅ 已修复（v3.4.1，2026-09-18 复核确认在位）

- **位置**：`backend/app/api/requirements.py:3721`（FP 合并）、`backend/app/core/task_selfheal.py:193`（自愈续跑）
- **问题**：裸 `asyncio.create_task(...)` 无强引用持有，CPython 事件循环只弱引用 task，GC 一跑协程可能静默消失，DB 任务永远停在 running（之后被 watchdog 误标 interrupted）。同文件 L107-111 的 `_create_bg_task` 是正确范式但未使用。
- **修复方案**：统一改用 `_create_bg_task`（selfheal 模块内建同款 helper）。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟡 M-1 Reranker 把检索结果硬截到 5 条，且被缓存放大 —— ✅ 已修复（v3.4.1）

- **位置**：`backend/app/db/hybrid_search.py:113-118`（截断）、`:125-129`（截断结果写缓存）
- **问题**：`RERANKER_ENABLED` 默认开（config.py:232），`rerank(..., top_n=MATCH_FINAL_N=5)` 无条件作用于融合结果，与请求 top_k 无关；截断后的结果按原 top_k 桶写入 24h 缓存。`top_k=20/40` 的检索最多返回 5 条，KB 召回率骤降且难排查。
- **修复方案**：rerank 后按 `max(top_k, MATCH_FINAL_N)` 截断或按 top_k 截断；缓存写完整 top_k 结果。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟡 M-2 KB 缓存失效点缺失，增删改后 24h 内返回旧结果 —— ✅ 已修复（v3.4.1，2026-09-18 复核确认在位）

- **位置**：`backend/app/db/kb_cache.py`（TTL=86400，LRU 无 TTL）；失效调用仅 `app/api/products.py:986/1525` 两处
- **问题**：`add_requirement`（7 个调用点）、`delete_requirement_from_kb`、`delete_cases_by_req_id` 等改变知识库内容的操作均不失效缓存。新归档需求"搜不到"、已删除需求"还在"。
- **修复方案**：在 add/delete 入口统一调 `invalidate_by_product`；LRU 条目加写入时间戳。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟡 M-3 BM25 降级路径缺 doc_type / project_id 过滤 —— ❌ 不修复（复核为误报）

- **位置**：`backend/app/db/hybrid_search.py:93-95`（调用未传）、`:133-254`（`_bm25_search` 签名无这两个参数）
- **原描述**：doc_type/project_id 过滤只在 Milvus 向量路径生效；向量失败仅剩 BM25 时 FP/总需求混入、其他项目数据穿透，且错误结果被缓存 24h。
- **复核结论（2026-09-17，详见复核报告）**：
  - `_bm25_search` **已接收并使用 `product_id`**，SQL 内含 `AND (CAST(:product_id AS VARCHAR) IS NULL OR product_id = :product_id)` —— 「其他项目数据穿透」不成立；
  - `requirements` 表**没有 doc_type 列**（运行时 `information_schema` 确认），功能点存于独立表 `functional_points`，而 BM25 是 `FROM requirements`，**不可能混入 FP 向量**；
  - 真实情况与该描述**方向相反**：降级到 BM25 时是**丢失 FP 召回**（少召回），而非混入。
- **结论**：两个风险均不成立，关闭。若确有"降级丢失 FP 召回"的业务诉求，需另行立项（为 requirements 增加 doc_type 维度，或让 BM25 联合检索 functional_points）。
- **修复记录**：不修复（误报）

### 🟡 M-4 限流器令牌桶双记余量时间，实际放行速率高于配置 —— ✅ 已修复（v3.4.1，2026-09-18 复核确认在位）

- **位置**：`backend/app/core/rate_limiter.py:75-83`
- **问题**：`refill = elapsed/window*limit` 全额入账后，`last_refill = now - (elapsed % window)` 把余量时间留给下一轮再记一遍，同一段时间被计入两次补充；`Retry-After` 也返回整个窗口而非距下一个令牌的时间。
- **修复方案**：连续模型 `tokens = min(limit, tokens + elapsed*rate)`，`last_refill = now`。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟡 M-5 管理日志按 UTC 边界过滤"当天"，本地 0~8 点数据查不到 —— ✅ 已修复（v3.4.1）

- **位置**：`backend/app/api/logs.py:29-50`
- **问题**：`_parse_date_start/end` 硬编码 `.replace(tzinfo=utc)`，中国时区用户筛选当天时漏掉本地 00:00~08:00 的日志。
- **修复方案**：按业务时区（如 Asia/Shanghai）换算成 UTC 边界再过滤。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟡 M-6 go_live_date 解析为 naive datetime，入库偏差 8 小时 —— ✅ 已修复（v3.4.1）

- **位置**：`backend/app/api/requirements.py:2332-2343`；`backend/app/api/products.py:687/1404`
- **问题**：`datetime.fromisoformat(...)` 结果无 tzinfo，直接赋给 `DateTime(timezone=True)` 列（models.py:65）。UTC+8 服务器上 naive 值按 DB 会话时区解释，偏差 8 小时。
- **修复方案**：解析后统一补 `.replace(tzinfo=timezone.utc)`（或按业务时区约定）。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟡 M-7 知识问答归档无幂等，重复提交产生重复知识条目 —— ✅ 已修复（v3.4.1，2026-09-18 复核确认在位）

- **位置**：`backend/app/api/knowledge_qa.py:847-990`（kqa_archive）
- **问题**：不检查 `record.archived`，重复点击/网络重试每次新建一条 archived Requirement 并重写向量库，`archived_id` 被覆盖，旧条目成孤儿。
- **修复方案**：`record.archived` 为 True 时 409 或直接返回已有 archived_id。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟡 M-8 kqa_ask 把请求级 session 传入长 SSE 生成器，耗尽连接池 —— ✅ 已修复（v3.4.1）

- **位置**：`backend/app/api/knowledge_qa.py:685-688`（传参）、`:562-563`（流尾才 commit）
- **问题**：LLM 流式输出数十秒期间一直占用 DI session，客户端读流慢/挂起时连接被长期占用，多并发耗尽连接池。
- **修复方案**：流内保存记录时新开 `async_session()` 短事务（参考 generate.py 的 `_update_gen_task`）。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟡 M-9 批量删除按需求只抽查第一条用例的产品归属 —— ✅ 已修复（v3.4.1）

- **位置**：`backend/app/api/testcases.py:453-458`、`:470`
- **问题**：`limit(1)` 取任意一条校验可见性后即删除该 req_id 全部用例；混合数据（迁移/修复产生）时会漏校验。
- **修复方案**：按 req_id 查 DISTINCT product_id 全量校验，或直接校验 Requirement.product_id。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟡 M-10 批量审核对不存在的 case_id 静默忽略 —— ✅ 已修复（v3.4.1，2026-09-18 复核确认在位）

- **位置**：`backend/app/api/testcases.py:577-610`
- **问题**：`WHERE id.in_(...)` 查不到的 ID 不报错也不计入 updated_count，前端误以为全部成功。
- **修复方案**：比对 found_ids 与请求列表，缺失时返回 not_found 列表。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟡 M-11 Celery"降级同步"是死代码，broker 缺省指向 localhost —— ✅ 已修复（v3.4.1，2026-09-18 复核确认在位）

- **位置**：`backend/app/tasks/celery_app.py:18-26`
- **问题**：`CELERY_BROKER_URL` 默认空串 + `or "redis://localhost:6379/1"` 兜底，`celery_app is None` 分支只在 celery 包 ImportError 时出现，不可达。未配 broker 的部署 `send_task` 必然连接失败（任务会被标记 pending_retry/failed 进 DLQ，不是静默挂起，但功能不可用）。
- **修复方案**：broker 为空时不创建 celery_app（保持 None 走同步降级），或启动时 ping 失败置 None。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟡 M-12 Celery 全局 time_limit=180 与任务级 240 冲突 —— ❌ 不修复（复核为误报）

- **位置**：`backend/app/tasks/celery_app.py:40` vs `backend/app/tasks/embedding_tasks.py:78`
- **原描述**：若全局硬超时生效，需求嵌入任务（soft 210/hard 240）会在 180s 被 SIGKILL，`except Exception` 捕不到，DLQ/failed 兜底失效。
- **复核结论（2026-09-17）**：Celery 的超时解析优先级为「调用级 > 任务属性/装饰器级 > 全局配置」。`embed_and_store_requirement` 已在装饰器显式声明 `time_limit=240 / soft_time_limit=210`，**全局 180 对它不生效**，不存在 SIGKILL 冲突——标题所述的"冲突"本身不成立。
- **结论**：关闭。全局 180/150 仅作为未显式声明超时任务的兜底（TC 任务显式声明 180/150，与全局一致），符合预期。
- **修复记录**：不修复（误报）

### 🟡 M-13 prompt_manager.get() 每次调用全量同步 DB 往返，阻塞事件循环 —— ✅ 已修复（v3.4.1，2026-09-18 复核确认在位）

- **位置**：`backend/app/core/prompt_manager.py:156`、`backend/app/db/prompt_store.py:47-77/119-154`
- **问题**：每次 `get()` 触发 `_init_registry()`（14 个注册项逐个同步 select+commit 的 upsert）+ `get_prompt` + `get_prompt_default`，至少 2~3 次同步 DB 往返且 `_ensure_table` 还要查 information_schema，全在 async 事件循环里阻塞。每次 LLM 调用前都发生。
- **修复方案**：注册只在启动和 update 后做；`get()` 加进程内缓存（update 时失效）；同步访问包 `asyncio.to_thread`。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟡 M-14 LLM 响应缓存 key 缺 response_format / max_tokens —— ✅ 已修复（v3.4.1）

- **位置**：`backend/app/core/llm_client.py:318-320`
- **问题**：key 只含 `agent|prompt|system|temperature|model`。同一 prompt 同 agent 既有 call（自由文本）又有 call_json（json 约束）且 temperature=0 时互相污染。触发面窄（需 prompt 完全相同）但真实。
- **修复方案**：key 加入 response_format、max_tokens、实际 provider。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟡 M-15 QualityGate 把缺 overall_score 当 0 分阻断 —— ✅ 已修复（v3.4.1）

- **位置**：`backend/app/core/quality_gate.py:200-207`
- **问题**：`quality_report.get("overall_score", 0.0)` —— 字典存在但缺 overall_score 键时按 0 分 Blocker。quality_report 为空字典/None 时会跳过（触发面比最初描述窄），clarify 降级分支产出半残 report 时误拦。
- **修复方案**：`score = quality_report.get("overall_score")`，`if score is not None and score < pass_score`。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟢 L-1 Markdown 导出表格单元格不转义换行，表格撕裂 —— ✅ 已修复（v3.4.1）

- **位置**：`backend/app/exporters/markdown.py:194-197`
- **修复方案**：单元格文本先 `replace("\r\n", " ").replace("\n", " ")` 再转义 `|`。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟢 L-2 Gherkin 导出步骤/标题含换行，生成非法 Gherkin —— ✅ 已修复（v3.4.1）

- **位置**：`backend/app/exporters/gherkin.py:107/113/129-131`
- **修复方案**：拼接前单行化（`\r?\n` → 空格）。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟢 L-3 XMind 导出控制字符未过滤，可能产出非法 XML —— ✅ 已修复（v3.4.1）

- **位置**：`backend/app/exporters/xmind.py:310-321`、`backend/app/exporters/req_xmind.py`
- **问题**：LLM 文本夹带 `\x00-\x08` 等 XML 1.0 非法字符时整个导出文件损坏。
- **修复方案**：统一 sanitize：`re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)`。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟢 L-4 markdown.py 使用已废弃的 datetime.utcnow() —— ✅ 已修复（v3.4.1）

- **位置**：`backend/app/exporters/markdown.py:74`
- **修复方案**：改 `datetime.now(timezone.utc)`。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟢 L-5 pagination 静默钳制 page_size 上限 100，调用方无感知 —— ✅ 已修复（v3.4.1；2026-09-18 补修前端遗漏调用方）

> **前端遗漏调用方补修（2026-09-18）**：`frontend/src/pages/TestCases.tsx` 与
> `frontend/src/pages/XMindEditPage.tsx` 此前均以 `reqApi.list(1, 300, ...)` 拉需求列表，
> 本修复把上限收到 100 后这两处必然 422（前端 catch 后静默变空，症状是"筛选下拉无数据"）。
> 两处均已改为**循环分页拉取**（每页 100，最多 10 页）。
> 缺陷编号与浏览器实测记录见 `TEST_REPORT.md §17.27` 的 **B-7** 行
> （实测：修前页面加载必现 422、需求标题回退；修后 422 消除、console 零错误）。

- **位置**：`backend/app/utils/pagination.py:34`
- **修复方案**：响应中回显实际生效的 page_size，或超限返回 422。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟢 L-6 Milvus 按产品/项目批量删除有 16384 条上限，超限静默漏删 —— ✅ 已修复（v3.4.1，2026-09-18 复核确认在位）

- **位置**：`backend/app/db/milvus_store.py:568-578/597-601/774-778/800-804`
- **问题**：单次 `query(limit=16384)` → delete → 返回 `len(ids)` 当作已全删。
- **修复方案**：循环 query+delete 直到空，或直接按 metadata 表达式 delete。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟢 L-7 编辑用例一律硬置 status="reviewed"，approved 降级、rejected 跳过驳回 —— ✅ 已修复（v3.4.1，改为仅内容字段变更时置 reviewed）

- **位置**：`backend/app/api/testcases.py:411-415`
- **修复方案**：仅 approved→reviewed 保留；rejected 的处理显式化。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟢 L-8 gen_batch_id 序号不按日重置，超 5 位撑破格式 —— ✅ 已修复（v3.4.1）

- **位置**：`backend/app/utils/id_gen.py:80-94`
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟢 L-9 case_dedup 前导零数字不归一，"第04次"与"第4次"不合并 —— ✅ 已修复（v3.4.1）

- **位置**：`backend/app/utils/case_dedup.py:42-44/108-110`
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟢 L-10 kb_cache 在 async 中使用同步 Redis 客户端，阻塞事件循环 —— 待修复

- **位置**：`backend/app/db/kb_cache.py:118-130/162-167`
- **修复方案**：改 `redis.asyncio` 或 `asyncio.to_thread` 包裹。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟢 L-11 Level-C 宽松兜底结果被缓存 24h —— 待修复

- **位置**：`backend/app/db/hybrid_search.py:215-240` + `:122-129`
- **修复方案**：记录命中 level，Level C 命中跳过缓存写（或短 TTL）。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟢 L-12 watchdog 需求回退仅 reclaimed>0 时执行，失败无补偿 —— 待修复

- **位置**：`backend/app/core/task_watchdog.py:76-98`
- **问题**：`reclaimed==0` 分支不回退 analyzing 需求；上一轮回退 update 失败时无补偿路径。
- **修复方案**：需求回退语句独立于 reclaimed 每轮幂等执行。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟢 L-13 FP 覆盖进度 total 分母漂移 —— 待修复

- **位置**：`backend/app/core/orchestrator.py:1019-1025`
- **修复方案**：total 固定 `len(fp_list)`，covered 只数 done/resumed。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟢 L-14 断路器只统计全 provider 失败，主 provider 挂时不熔断 —— 待修复

- **位置**：`backend/app/core/llm_client.py:347-395`（record_failure 时机）、`110-120`（HALF_OPEN 无并发探测限制）
- **修复方案**：provider 级失败同步计入 breaker；HALF_OPEN 限同时探测数=1。
- **修复记录**：✅ 已修复（改动明细与验证见文末「修复进度统计」）

### 🟢 L-15 流式"不支持流式"误判过宽 —— ✅ 已修复（v3.4.3）

- **位置**：`backend/app/core/llm_client.py`（原 `:1157-1159`）
- **问题**：原判定为「错误里含 `stream` 且含 `support` / `invalid` / `unknown` / `not found`」，
  过宽——如 `stream error: api key not found`（**认证问题**）也会被判成"厂商不支持流式"，
  白跑一次非流式回退，且日志把真因写成流式问题，把排查方向带偏。
- **修复**：改为命中 `STREAM_UNSUPPORTED_PHRASES` **精确短语白名单**才算（9 条厂商常见措辞，
  如 `does not support streaming` / `streaming is not supported` / `unsupported parameter: stream`）。
- **验证**：8 个错误样本全部符合预期 —— 3 条真不支持 → 判为需回退；4 条误报场景
  （认证 / 超时 / 未知参数 / 模型不存在）→ **不再误判**；1 条精确短语 → 正常命中。
- **修复记录**：v3.4.3 commit（见 git log）

### 🟢 L-16 生成阶段 impact 节点重复执行影响分析 BFS，且与人工确认的影响范围存在漂移风险 —— 已修复（2026-09-20）

- **位置**：`backend/app/agents/impact_agent.py:43-121`（`analyze()` 主入口，第②③步）；`backend/app/core/orchestrator.py:715-748`（`_impact_node`）
- **问题**：需求分析阶段已通过 `analyze_requirement_impact()`（`impact_agent.py:615-833`，模块 BFS + FP 双向 BFS）产出受影响 FP 候选，经人工确认后写入 `srr.impact_analysis`（`is_affected=True 且 confirmed_at 非空`）。但生成 DAG 的 impact 节点对优化需求会**重新执行一遍**模块依赖图 BFS（`_analyze_module_impact`）与受影响需求定位（`_find_regression_requirements`，按 `Requirement.module` 模块名匹配）：
  1. **重复计算**：同样的边表、同样的 ≤3 跳遍历跑两次，多两趟全表加载；
  2. **口径不一致**：需求分析阶段是 FP 级精度（`FunctionalPoint.module_id` + fan_in 排序 + 同名 FP 去重），生成阶段是 Requirement.module 字符串匹配，两个模块归属口径可能对不上；
  3. **一致性风险（最关键）**：两次 BFS 之间若依赖边被增删（如新依赖通过审核），生成阶段现算的范围可能与**人工确认过的影响范围不一致**，相当于用机器新算的结果悄悄覆盖了人工背书的结论。而消费端 RTS 回归装配（`orchestrator.py:843-938` `_assemble_regression_cases`）本来就只认 `srr.impact_analysis` 的确认条目——重算的 BFS 结果仅流入 `generation_guidance` 文本与 done 事件报告，实际价值很低。
- **建议方案**：`analyze()` 前半段改为直接消费 `srr.impact_analysis` 中已确认条目（FP 名单 / 归属需求 / 模块名均已在条目内），删除重复 BFS；`_find_regression_requirements` 的模块名匹配可换成确认条目里现成的 `req_id`/`req_title`。**保留**后半段 diff 驱动的用例操作规划（`_plan_case_operations` / `_plan_regression` / `_build_guidance`）——其输入 `optimization_context.diff` + 原需求历史用例是需求分析阶段没有的，不可替代。
- **收益**：少两趟全表加载 BFS；消除双口径不一致；影响范围与人工确认严格一致（消除漂移）。
- **影响面**：仅 `impact_agent.analyze()` 前半段，不动 API 层与 DAG 结构；`_analyze_fp_impact`（FP 单点分析，API 触发）不受影响。
- **2026-09-20 复核结论：成立，且比条目所写更严重（证据已入库核实）**
  1. **加重证据·生成侧 BFS 实为空转**：`Requirement.module` 列存的是**模块名**（实测 '工单管理'/'智能问答'），而依赖边表 `ModuleDependency` 的写入侧（`products.py:2119-2127`）按 **Module.id**（'MOD-856-…'）校验存键。生成侧 `analyze()` 用 `involved_modules`（模块名）做 BFS 起点，在 ID 键控的邻接表上**一步都走不动**（线上边表现有 0 条，即使有也匹配不上）→ `_analyze_module_impact` 恒返回 direct=模块名、indirect=∅。`_find_regression_requirements` 的 `Requirement.module.in_(affected)` 退化为"同名单模块内其他需求"的字符串碰撞，与依赖图无关。
  2. **对照确认**：需求分析侧 `analyze_requirement_impact()`（:615）FP 精度、人工确认、守卫完备（generate.py:151-165 拒绝未确认条目），条目形状实测含 `fp_id/fp_name/module_id/source_req_id/is_affected/confirmed_at`——生成侧"消费确认条目"所需的全部字段**现成可用**。
  3. **方案裁定**：条目原方案（删 BFS、消费确认条目）**直接采纳**。另需补两点条目未覆盖的边界：① `impact_analysis` 为空的优化需求（从未跑过影响分析）当前**允许生成**（守卫只拦"存在未确认条目"）——重构后该场景 fallback 为"无确认影响"并打 `degraded_flags` 提示（现状是 BFS 空转出同样结果，行为不变、可观测性提升）；② done 事件 `impact_result` 形状（`test_phase4:715-721` 断言 is_optimization/generation_guidance）保持不变。
- **修复记录（2026-09-20）**：已按方案裁定实施——`analyze()` 前半段改为纯消费 `srr.impact_analysis` 确认条目（优化需求路径零 DB 调用），删除生成期 `_find_regression_requirements`；`impact_unconfirmed` 标记经 `_impact_node` 透传 `degraded_flags`；orchestrator 类 docstring 的过时 FULL/FAST 叙述顺带修正。回归全绿（unit 117/1、phase2 74、phase3 91、phase4 151、phase5 101、phase6 141，与基线一致）。commit 见 git log "L-16"。

### 🟡 L-17 ReviewAgent 审核链路的分层低效：执行顺序倒置、LLM 职责与输入错配、无分批能力 —— 方案 A+B+C 已全部修复（2026-09-20）

> 来源：2026-09-20 审核链路（review 节点）代码走查（对应 `review_agent.py` / `orchestrator.py _review_node`）。
> 分层职责划分本身正确（确定性检查已前置、LLM 只做语义判断、发布决策带证据复核），
> 问题集中在**执行顺序、信息配比、规模扩展性**三处。

- **位置**：`backend/app/agents/review_agent.py:102-215`（`review()` 主流程）、`:221-250`（`_calculate_confidences`）、`:330-404`（`_llm_review`）、`backend/app/prompts/review_agent.py:128-165`（prompt 降载）
- **问题**（三处独立，可分别处理）：
  1. **执行顺序倒置（最省事的修复）**：`review()` 按 ①置信度计算（每条用例一次向量检索，N 次 I/O，最贵）→ ②规则预检（零成本）→ ③LLM 六维 的顺序执行。已判 blocker 的用例（如缺 related_requirement）照样做了向量检索、照样进 LLM 清单。blocker 占比高时（畸形生成/供应商异常）白烧 N 次检索并污染 LLM 输入。应改为：规则预检先行 → blocker 出局 → 存活用例才做置信度检索与 LLM 审核。
  2. **LLM 职责与输入错配**：v3.1.3 prompt 降载后（正确方向，避免 200K+ 卡死），每条用例仅剩 8 个精简字段（`steps`=数量、`assertions`=数量、`expected` 截 160 字），LLM 实际只能做"集合级诊断"（组合合理性/类型分布/与需求概要的语义一致性），却仍以"六维深度审核"的名义产出 issues——名义职责 > 输入支持的职责。单条用例质量实际只有规则预检在管，中间层是空的。
  3. **单次全量调用无分批**：条目全量进一次 LLM 调用（精简后每条约 60-80 token）。当前用例数（FP 分片生成，通常几十条）安全；但大需求下 集成 + 多类型 + 补生成合并后可上探 300-500 条（20-50K），逼近危险区。与生成端不对称：**生成做了 FP 分片，审核没有**。置信度检索（N 次逐条 `search_similar_cases`）同样在大用例集下先成为瓶颈——可仿照生成端 L4 去重的做法（`embed_texts` 批量一次 + 内存余弦）批量化。
- **建议方案**（按性价比排序）：
  - **B（零风险）**：重排执行顺序（问题 1），纯顺序调整不改语义；
  - **C（纯工程）**：置信度检索批量化（问题 3 后半），`_calculate_confidences` 与模式库检索改为批量 embedding + 内存计算；
  - **A（治本，观察后立项）**：审核同构走 FP 分片（问题 2+3 前半）——按 `related_fp` 分组每组一次小审核（组内可放宽字段截断、放回步骤前 2 步，小 prompt 允许富信息），再补一次跨组集合级小审核（仅标题+类型+分布，判跨组重复/组合问题）。调用次数 1→N+1 但每次小、可并行；顺带解决 LLM 审核的语义矛盾。
- **不建议**：回退全量 prompt（200K 卡死是实测过的）；砍掉 LLM 六维只留规则预检（语义一致性判断规则写不出来）。
- **触发观察指标**：线上任务用例数分布（>300 条占比）与 review 节点耗时 / LLM 首 token 时长，超过阈值再启动方案 A。
- **2026-09-20 复核结论：三个子问题全部成立（证据已入库/读码核实）**
  1. **顺序倒置属实**：`review()` 实际顺序 = ①`_calculate_confidences`（:170，逐条 `search_similar_cases`，N 次 Milvus I/O）→ ②`_rule_based_review`（:183，纯内存零成本）→ ③`_llm_review`（:190）。blocker 判定在 ② 才出现，已注定 blocker 的用例已白付 ① 的向量检索、且照样进 ③ 的 LLM 清单。
  2. **输入错配属实**：`prompts/review_agent.py:139-154` 实测每条用例仅 8 字段（id/title/type/priority/related_fp/steps=数量/assertions=数量/expected 截 160 字），LLM 无从执行"步骤级语义审核"——名义六维、实际集合级诊断。单条质量检查只剩规则预检（blocker 仅 traceability 一类 + executability warning/info），中间层确为空。
  3. **无分批属实 + 规模实测**：`_llm_review` 单次 `call_json` 全量注入。线上分布（按入库日聚合）：单日最高 397 条入库（2026-09-03），p50≈89、最大 737（累计口径）——**L-17 预估的 300-500 条区间已被线上实际触及**（390+ 条日已有两日），方案 A 的"观察后立项"前置条件已满足，不必再等。
  4. **补充证据·置信度消费方**：case 级 `confidence` 下游有**三处硬依赖**——`persist_to_kb` 入库分档（≥0.70 auto_approve，review_agent.py:850）、标题级去重保留高分（:801-804）、列表筛选 `min_confidence`（api/testcases.py:82-125）。**方案 B 重排顺序时 blocker 用例的 confidence 计算不能省**（去重与落库仍需），只省向量检索与 LLM 输入——即 blocker 用例需一个不查 KB 的降级置信度路径（gen_self+completeness 因子仍可算，match_score=0），否则标题去重会拿 0 分错杀高质 blocker。
- **修复记录（2026-09-20，方案 B+C）**：已实施——①重排为 规则预检→置信度→LLM，blocker 用例跳过 KB 检索（`skip_search_ids`，置信度走冷启动 quality_base 兜底照算）且剔除出 LLM 清单（`exclude_case_ids`），report 全量口径不变；②新增 `milvus_store.search_similar_cases_batch()`（一次批量 embed + pymilvus 2.4 多向量 search，N 次 embed+N 次 RPC → 1+1），批量失败回退逐条。签名向后兼容，回归全绿（同 L-16 口径）。commit 见 git log "L-17"。
- **修复记录（2026-09-20，方案 A）**：已实施——`review_agent.py` 新增 FP 分片审核调度器 `_llm_review_shards()`：按 `related_fp` 分组（空标注进 "" 兜底桶，线上实测 23.5% 用例无标注）→ 每片一次**组内富信息审核**（`prompts.build_group_review_prompt`：放宽至前置条件 + 前 2 步 action/expected + 预期 200 字，恢复步骤级语义审核能力；声明"跨组问题勿在此报告"避免与汇总审核重复上报）→ 一次**跨组集合级审核**（`prompts.build_cross_group_review_prompt`：仅各片标题+类型分布，判跨组重复/组合矛盾/分布失衡，未知 case_id 拒收防 LLM 编造）。规模开关 `_should_use_shard_review`：非 blocker ≥24 条**且**分片数 >1 才走分片（小集合/单 FP 保留单发，避免 N+1 次调用的固定开销反噬）；并发上限 `_SHARD_CONCURRENCY=4`（信号量）。**失败语义（fail-closed 落终态）**：部分分片失败→不降级，已成功分片证据保留，报告打 `review_shard_partial_failure` + `shard_review.failed_shards` 明细；全部分片失败→`review_degraded=True`（`shard_review_all_failed`，发布策略按 review_llm_failed 拦截自动发布）；跨组审核失败→组内证据保留 + info 级提示（不静默）。avg_confidence 按分片用例数加权（口径与单发一致）。容器内桩测 8 分支全过（全成功/部分失败/全失败/跨组失败/编造 case_id 拒收/全 blocker/规模开关三态）+ 全量回归绿（unit 117/1 + phase2 74 + phase3 91 + phase4 151 + phase5 101 + phase6 141）。commit 见 git log "L-17 方案A"。
- **状态更新：L-17 三个子问题（顺序倒置/输入错配/无分批）全部修复**。监控建议：上线后观察 `shard_review.failed_shards` 出现频率与分片审核耗时（日志 path=fp_shards），若部分失败率持续偏高再考虑失败分片自动重试（当前一次即终态）。

---

## 二、待进一步确认上下文的问题（复核时未逐行验证）

| 编号 | 位置 | 简述 | 状态 |
|------|------|------|------|
| P-1 | `core/orchestrator.py:1072` vs `core/pipeline_fingerprint.py:170` | 续跑指纹 product_id 两侧来源不一致（srr vs DB），旧数据可能每次续跑 409 | **已修复（v3.4.3，见 §六）** |
| P-2 | `core/pipeline_fingerprint.py:57-60` | fp_set_sha256 只哈希 FP id，改名后续跑覆盖率矩阵失真 | 待确认 |
| P-3 | `api/knowledge_qa.py:997-1154` | 问答历史/详情/corrections 缺产品可见性过滤 | 待确认 |
| P-4 | `api/knowledge_qa.py:1245-1406` | approve_correction 向量先写、DB 状态靠请求尾 commit，失败可重复重嵌 | 待确认 |
| P-5 | `api/testcases.py:128/169` | `_extract_fp` "业务场景"魔法字符串过滤 | 待确认 |
| P-6 | `exporters/pytest_code.py:195` | LLM 返回 type 未净化，可能生成非法 Python | 待确认 |
| P-7 | `core/llm_client.py:268/296/901` | 循环变量遮蔽 provider 参数；docstring 返回值与实际不符 | 待确认 |
| P-8 | `core/requirement_tasks.py:954-955` | `_update_task` 吞掉所有异常，状态可能长期失真 | 待确认 |
| P-9 | `core/task_watchdog.py:54-71` + `requirement_tasks.py` | watchdog 60 分钟阈值对"慢而活"的 LLM 调用仍有误杀盲区（机制属实，触发概率中等）；建议加心跳 | 待确认 |
| P-10 | `core/rate_limiter.py:113` | 反代下所有用户共享一个限流桶（内网直连部署无影响） | 待确认 |
| P-11 | `db/hybrid_search.py` Level C + `db/kb_cache.py` | （并入 L-11） | 待确认 |

---

## 三、复核后判定为误判 / 不成立的问题

（暂无——最初报告中的安全问题（JWT 无吊销、密码策略等）用户已明确暂不处理，见 `SECURITY-AUDIT-REPORT.md` 相关记录。）

---

## 四、真实测试新发现的缺陷（v3.4.2）

> 来源：修复完成后对真实服务的端到端验证（真实 HTTP + 真实 DB/Redis/Milvus）。
> 这两条在静态复核与四套回归中都**没有暴露**——回归用例没有覆盖对应路径。
> 教训：**"用例全绿"不等于"功能可用"**，修复后必须用真实数据跑一遍。

### 🔴 N-1 confirmed 需求提交澄清必然 500 —— 已修复

- **位置**：`backend/app/api/requirements.py`（`submit_clarify_answers` 的 confirmed 分支）
- **问题**：原写作 `_func.count(FunctionalPoint.id).where(...)`。`count()` 返回的是函数
  表达式，**没有 `.where` 方法**，必然抛
  `AttributeError: Neither 'count' object nor 'Comparator' object has an attribute 'where'` → 500。
- **影响**：只要需求处于 confirmed 且用户提交澄清，请求直接 500。这也是 H-1 所述
  "confirmed 澄清失败后回退成 draft"**根本走不到**的原因——在取快照之前就炸了。
  换言之：H-1 的 bug 被这个更前置的 500 掩盖着。
- **为何长期未被发现**：该分支只在 `record.status == "confirmed"` 时进入；
  phase5 回归只覆盖了 draft 路径；静态复核时注意力在快照时机，未逐行审这一段。
- **修复**：改为 `select(func.count(...)).where(...)`。
- **真实验证**：`POST /api/requirements/{confirmed_req}/clarify` → **200**，
  快照 `original_status='confirmed'`，任务正常创建（修复前该请求 500）。

### 🟢 N-2 pytest 导出未过滤控制字符，整份文件无法编译 —— 已修复

- **位置**：`backend/app/exporters/pytest_code.py`（`_sanitize_docstring` / `_sanitize_comment` + 出口）
- **问题**：用例正文（描述/步骤/预期）夹带 `\x00` 等控制字符时被原样写进生成的 `.py`，
  `compile()` 直接报 `source code string cannot contain null bytes`，**整份导出不可用**。
  与 L-3（XMind/XML）属同类问题，但此前只防护了 XML 侧。
- **修复**：新增 `_strip_control()`（保留 `\n` `\t`），在净化入口与整份产物出口各过滤一次。
- **真实验证**：创建含 `\x00\x08\x07`、换行、`type="边界 异常"` 的用例 → 导出 pytest
  → `compile()` 通过，`@pytest.mark` 名净化正确。

### 🔴 N-3 LLM 供应商全挂时调用悬挂数十分钟且无终态提示 —— 已修复

- **位置**：`backend/app/core/llm_client.py`（`call()` 的 provider 循环、`_call_with_retry`）
- **问题**：供应商全部不可用时（免费额度耗尽 403 / 限流 429 / 首响应超时），原实现**逐家串行**
  等待 `LLM_FIRST_RESPONSE_TIMEOUT=180s`（5 家 × 180s ≈ 15 分钟），再叠加每家的 5 次重试，
  单次调用可悬挂**数十分钟**。表现：生成任务进度停滞、无任何终态提示，且一直占用该需求的
  生成互斥槽位（重新发起返回 409），只能人工取消或等看门狗 60 分钟介入。
  与"fail-closed 要落成终态"的纪律相悖。
- **修复**：
  1. 新增 `settings.LLM_CALL_TOTAL_BUDGET`（默认 **240s**）：单次 `call()` 的总墙钟预算，
     跨 provider × 跨重试累计；超预算立即停止尝试剩余 provider 并报错。
  2. 首响应等待受剩余预算约束（`min(180, 剩余)`），不再"每家都等满 180s"。
  3. **硬失败立即禁用**：额度耗尽 403 / 认证失败 401 / 4xx 等（`_NonRetryableError`）
     直接禁用该 provider 600s，不再等累计 3 次 → 后续调用直接跳过它。
  4. 错误信息明确指引：*"请到「系统设置 → LLM 供应商」更换可用供应商，或检查 API Key / 额度"*，
     并附各家失败明细（如 `qwen2 403 Free quota exhausted；日日新 首响应超时`）。
  5. 区分两个分支：**"候选全在禁用期"** 与 **"未配置 API Key"** 分别给提示
     （此前一律报"未配置 API Key"，把排查方向带偏）。
- **真实验证**：
  | 场景 | 结果 |
  |------|------|
  | 全部候选预禁用 | **0.02s** 失败，提示含"更换可用供应商"，并列出处于禁用期的 5 家 |
  | 2 家不可达 provider + 15s 预算 | **14.1s** 失败（日志：`调用总预算 15s 已耗尽，跳过剩余 1 家提供商`） |
  | 真实环境 qwen2（403 额度耗尽） | 被**立即禁用 600s**，故障转移到可用供应商并成功返回 |
- **附注**：连接类失败（不可达 / 超时）仍按既有设计累计 3 次后禁用（`PROVIDER_FAILURE_THRESHOLD`）——
  这是合理的（给偶发抖动留机会），且已受总预算保护，不会悬挂。

### 🔴 N-4 检查点后端降级标记零消费方（docstring 承诺的「done 事件可见」是假承诺）—— 已修复

- **位置**：`backend/app/core/orchestrator.py`（`_ensure_checkpoint_backend` :364-404、`run_pipeline` 入口）
- **问题**：`_ensure_checkpoint_backend()` 在 `AsyncPostgresSaver` 初始化失败时置
  `self._checkpoint_backend_degraded = True`，其 docstring 明确承诺
  *「memory：…（日志 + `_checkpoint_backend_degraded` → done 事件的 degraded_flags 可见），绝不静默」*。
  **但该标记全库只有定义（:300）与赋值（:397）两处、零个读取方**——docstring 描述的行为
  从未存在。后果：检查点静默退化为 `MemorySaver`（进程重启即丢断点、断点续跑失效），
  运维在 done 事件、任务详情、发布决策里**全部看不到任何痕迹**。
- **触发条件**：`PIPELINE_CHECKPOINT_BACKEND=postgres`（默认）且 PG 连接/建表失败时——
  与 §I「依赖漂移」是同一治理面，属"降级发生了但不可观测"。
- **修复**：
  1. 新增模块级纯函数 `initial_degraded_flags(checkpoint_backend_degraded) -> list[str]`
     （与 `make_sse_event` 同层，便于确定性单测）；
  2. `run_pipeline` 入口把它注入 `initial_state["degraded_flags"]`，之后各节点沿用既有
     `list(state.get("degraded_flags", []) or []) + append` 形态逐级透传到 done 事件；
  3. 入口追加 `logger.warning`（运维可见）；
  4. 因 `publication_policy.decide_publication` 会把 `degraded_flags` 并入 `reasons`，
     降级项自动进入 `human_review`，**不会被自动核准**（fail-closed 语义顺带成立）。
- **关键前提**：`degraded_flags` 必须在 `PipelineState` 显式声明（已在 :140），
  否则 LangGraph 静默过滤未声明字段，入口注入的标记会被丢弃（同类已踩 5 例）。
- **守卫断言**（`test_phase4_orchestrator.py::test_initial_degraded_flags`，5 条）：
  未降级不注入 / 降级注入正确 flag / done 事件真的带出来 / 进入 publication.reasons /
  `decision != auto_approve`。**覆盖"写了 → 到达消费方"的完整链路**，而非只断言变量被赋值。
- **遗留**：当前仍是「降级 + 透出」，未改为**直接抛错 fail-closed**——保留 memory 降级
  是为让单机/演示环境可用。是否升级为硬失败待拍板（见下方"待确认"）。

### 🔴 N-5 FP 提取失败静默降级为 0 个功能点，任务仍 completed —— 已修复

- **位置**：`backend/app/agents/req_agent.py::_fp_extract_from_raw` :841-1086；
  `backend/app/core/requirement_tasks.py`（三入口）；`backend/app/api/requirements.py`（finalize）
- **问题**：FP 一体式提取的 LLM 调用失败时（首响应超时 / 返回类型异常），原实现仅
  `logger.warning` 后 `return [], [], None`。调用方日志写「FP 提取完成: 0 个功能点」，
  任务照常 `completed`。**用户可见面只有一个干净的「0 功能点」，没有任何失败证据**，
  无法区分「需求本身确实没有功能点」与「LLM 挂了」；而 0 FP 会让 FP 分片生成通道
  整体退化为全量兜底（覆盖率分母缩水）。
  这正是 `docs/16` 审计报告 **A2 条目**（「FP 提取为空无降级标记」，状态曾为"待实现"）的落地；
  2026-09-28 的真实端到端验收再次实测复现（日志：`[LLM] 首响应超时: provider=日日新…`）。
- **修复**：
  1. `_fp_extract_from_raw` 内新增 `_mark_degraded(reason)`：失败时把
     `_fp_extract_degraded` / `_fp_extract_error` 打在**传入的 SRR 字典**上。
     ⚠️ 必须打在 SRR（任务私有对象）而非 `self.xxx` 实例属性——`req_agent` 是全局单例，
     实例属性会跨任务并发污染。
  2. 覆盖**两个失败出口**：`except` 分支（LLM 异常）与 `if not isinstance(result, dict)`
     分支（LLM 返回 null/字符串），不能只改其一。
  3. 读取必须在**调用点**用局部变量捕获，不能在出口读 `srr_json_data`——
     主入口出口的 `srr_json_data` 可能已被替换为 ClarifyAgent 的 `augmented_srr`
     （一个**新字典**，不带 ReqAgent 打的标记）；`_run_clarify_reparse` 原先还**漏传 `srr=`**
     （默认 None），标记根本打不到任何地方，一并补上。
  4. 三入口（`_run_requirement_analysis` / `_run_clarify_reparse` / `_run_regenerate_reparse`）
     与 finalize 共 **4 个出口** 透出 `fp_extract_degraded` / `fp_extract_error` + `degraded` / `degraded_reason`。
  5. **失败路径对称透出**：三个入口的 `except` 分支原本整体不写（或部分不写）`result_json`，
     任务 `failed` 时标记被整个丢掉——首版实现正犯了这个"只覆盖 happy path"的错，
     补上后收敛为**单一助手 `_apply_fp_extract_degrade()`**（6 处调用点共用），
     使"漏改 except"从 6 段相似代码的人工比对，变成对同一函数调用完整性的检查。
  6. 三处 `fp_extract_degraded` 初始化移到 `try` **之外**——否则任务整体失败走 `except` 时
     引用未定义变量会抛 `NameError`，把「LLM 不可用」变成「代码崩溃」。
- **守卫断言**（`test_phase2_html_upload.py::test_fp_extract_degrade`，9 条，**注入异常验证**，
  不等自然发生）：注入 `TimeoutError` → 返回空 FP 不抛异常 / SRR 打上标记 / 携带真实原因 /
  completed 路径透出 / 失败路径透出 / 文案点明「不是需求没有功能点」/ 未降级不写标记（防假降级）/
  已有更具体 `degraded_reason` 时不被空文案覆盖。
- **口径说明**：`req_agent.refine_srr()`（:97-229）内也调用了 `_fp_extract_from_raw`，
  但该路径**全库无调用方（死代码）**，标记随 `srr` 自然带上，未额外接透出。

---

## 五、真实生成链路的观察（2026-09-17）

> 来源：为验证 L-13（FP 进度分母）而发起的真实生成任务。观察结论用于后续迭代排期，
> **不是**本次修复引入的问题。

### 观察 1：LLM 供应商全部不可用时，生成任务长时间悬挂且无终态 —— **已由 N-3 修复**

> 现象记录保留在下方，修复方案与验证见 §四 N-3。

实测日志（同一任务的连续片段）：

```
07:31:14  [GenAgent] 调用 LLM: temperature=0.4, max_tokens=32768
07:34:16  [LLM] 首响应超时: provider=qwen:qwen3.8-max, 180s 内无任何响应，切换备用提供商
07:35:19  [LLM] 调用失败 [attempt=1/5], 1.43s 后重试: RateLimitError 429 - inference exceeds tpm
07:38:39  [LLM] 首响应超时: provider=日日新:sensenova-6.8-flash-lite, 180s 内无任何响应，切换备用
07:38:39  [LLM] 提供商 qwen1:deepseek-v4-flash-0731 不可重试错误，故障转移: 403 Free quota exhausted
07:38:47  [LLM] 调用失败 [attempt=1/5], 1.74s 后重试: RateLimitError 429
```

- **表现**：`180s 首响应超时 × 多 provider 轮盘 × 5 次重试` 叠加，单次 LLM 调用可耗时数十分钟；
  任务期间进度停留在 70%（`generate` 阶段）、`updated_at` 长时间不刷新，且**没有任何终态提示**。
- **副作用**：该任务一直占用该需求的生成互斥槽位，其他用户无法为该需求重新生成
  （实测重新发起返回 409）；只能人工到任务管理页取消，或等看门狗 60 分钟后标记 `interrupted`。
- **与既有纪律的冲突**：按"fail-closed 要落成终态，不是异常"的口径，**"所有 provider 都不可用"
  应有快速失败判定并落成终态**（如 `failed` + 明确 reason），而不是让它悬挂到超时。
- **建议**：单次 LLM 调用的总预算（所有 provider × 所有重试之和）设上限，超预算即返回
  终态错误；前端在 `generate` 阶段长时间无进展时给出"供应商不可用"的明确提示。
  与 P-9（看门狗 60 分钟误杀"慢而活"任务）是同一治理面的两端。

### 观察 2：L-13 未能完成端到端观测的原因

- 目标需求 `REQ-1789000785081-9973` 的 2 个 FP 均为 `approved`，但本次生成的 GenAgent 日志为
  `fp_count=0`、走的是"无功能点兜底/集成"路径，**不产生 `_emit_gen` 的 FP 分片进度事件**，
  因此 15 分钟内采集不到任何 `fp_progress`（不是修复未生效，而是该路径不产出该事件）。
- 叠加观察 1 的供应商问题，生成无法在合理时间内跑到 FP 分片阶段。
- **结论**：L-13 的修复逻辑（分母固定为 `len(functional_points)`）可由代码确认，但要看真实
  `fp_progress` 事件，需要一个**确实走 FP 分片路径的需求 + 可用的 LLM 额度**，建议在下次
  实际使用生成功能时顺带观察，或等供应商额度恢复后重跑 `verify_l13_progress.py`。

---

## 六、暂缓项转已修复（v3.4.3，2026-09-18）

> 原"暂缓"的 5 项中，**P-1 与 L-15 的前置条件已具备，本次修掉**；L-14 / P-4 / P-9 仍单独立项。

### 🟡 P-1 续跑指纹两侧取值不同源 —— ✅ 已修复

- **机制**：写 checkpoint 时 `product_id` 取 **SRR JSON 内的字段**（`orchestrator.py`），
  而续跑校验时从 **DB 列**取（`pipeline_fingerprint.load_current_fingerprint`）——
  两者不同源，一旦不一致，续跑必然 409。
- **普查（修复前实测，只读 SQL）**：

  | 指标 | 数值 |
  |------|------|
  | 需求总数 | 42 |
  | SRR 内 `product_id` 为空 | 13 |
  | 两者不一致 | 13 |
  | 带 checkpoint（可续跑）的任务 | **3** |
  | 其中分叉、续跑必 409 | **0** |

  → **存量零分叉、无迁移负担**（33 个任务中仅 3 个带 checkpoint，且都一致）；
  风险面在"今后"：那 13 条需求（31%）一旦生成中断后续跑就会踩到。
- **修复**：写入侧改为**复用校验侧的同一个函数** `load_current_fingerprint`
  （它从 DB 取 product_id / revision / srr_json / FP 集）→ 两侧天然同源，分叉在结构上不可能再发生。
- **验证**（真实需求）：

  | 需求 | DB.product_id | 修复前（取 SRR） | 修复后 |
  |------|---------------|------------------|--------|
  | REQ-1787889103095-8410 | `PROD-TEST` | `''`（**分叉**） | `PROD-TEST` ✓ |
  | REQ-1788312804982-7233 | `PROD-77712-469` | `''`（**分叉**） | `PROD-77712-469` ✓ |

### 🟢 L-15 流式"不支持"判定过宽 —— ✅ 已修复

详见 §一 L-15 条目（已更新为已修复状态）。

---

## 修复进度统计（v3.4.1 + v3.4.2 + v3.4.3，2026-09-18）

| 严重程度 | 总数 | 已修复 | 误报关闭 | 暂缓 |
|---------|------|--------|---------|------|
| 🔴 高 | 5 | 5 | 0 | 0 |
| 🟡 中 | 15 | 13 | 2（M-3 / M-12） | 0 |
| 🟢 低 | 15 | **14** | 0 | **1（L-14）** |
| **合计** | **35** | **32** | **2** | **1** |

**真实端到端测试另新增 3 条（N-1 / N-2 / N-3），均已修复** —— 即累计 **38 条中已修复 35 条**。
真实测试通过项：27 项（22 项接口/服务层 + 5 项 H-1/H-4/M-8 链路）+ N-3 的 3 个场景验证。

**P 系列（11 条）**：已修复 **7**（P-1 / P-3 / P-5 / P-6 / P-7 / P-8，P-11 并入 L-11）、
不修 2（P-2 有意设计 / P-10 部署决策）、暂缓 2（P-4 架构改造 / P-9 心跳机制）。

### 本次修复清单（v3.4.1）

| 批次 | 条目 | 关键改动 |
|------|------|---------|
| 高危 | H-1 | `requirements.py` 状态快照提前到任何状态修改之前（原在 confirmed→draft 改写之后取，失败回退成 draft） |
| 高危 | H-2 | `testcases.py` 手工建用例改用 `gen_test_case_id()`（原秒级时间戳同秒撞主键） |
| 高危 | H-3 | `models.py` + `database.py` 为 `test_cases` 补 `vectorization_status/error` 两列（原赋值静默丢弃，前端永久幽灵 pending） |
| 高危 | H-4 | `requirements.py` submit_clarify / regenerate 状态变更与任务插入合并为一次 commit（消除 advisory lock 释放窗口） |
| 高危 | H-5 | `requirements.py` / `task_selfheal.py` 裸 `create_task` 改带强引用的 `_create_bg_task` |
| 中危 | M-1 | rerank `top_n = max(top_k, MATCH_FINAL_N)`，不再写死截到 5 条 |
| 中危 | M-2 | `milvus_store` 写入/删除后同步失效 KB 缓存；`kb_cache` 新增 `invalidate_by_product_sync` |
| 中危 | M-4 | 限流器改连续补充模型（实测放行 **16 次 → 3 次**/秒，Retry-After 改为距下一令牌） |
| 中危 | M-5 | 管理日志日期筛选按 Asia/Shanghai 解释后再转 UTC |
| 中危 | M-6 | `requirements.py` / `products.py` 的 go_live_date 补 tzinfo（第三处此前已修） |
| 中危 | M-7 | `kqa_archive` 增加幂等（已归档 409） |
| 中危 | M-8 | SSE 生成器不再持有请求级 session，改独立短 session |
| 中危 | M-9 | 批量删除按 `Requirement.product_id` 校验（原 `limit(1)` 抽查任意一条） |
| 中危 | M-10 | 批量审核返回 `not_found` 列表并在 message 中提示 |
| 中危 | M-11 | `CELERY_BROKER_URL` 为空时不创建实例，走同步降级（原 localhost 兜底使降级不可达） |
| 中危 | M-13 | `prompt_manager` 加进程内缓存 + registry 只初始化一次 + `_ensure_table` 只建一次（实测热路径 **50 次取值 0 次 DB 往返**） |
| 中危 | M-14 | LLM 缓存 key 补 `response_format` / `max_tokens` |
| 中危 | M-15 | QualityGate 缺 `overall_score` 不再按 0 分拦截 |
| 低危 | L-1 / L-2 | Markdown 单元格、Gherkin 步骤统一单行化后再转义 |
| 低危 | L-3 | XMind / req_xmind 导出过滤 XML 1.0 非法控制字符 |
| 低危 | L-4 | `datetime.utcnow()` → `datetime.now(timezone.utc)` |
| 低危 | L-5 | requirements 列表 `page_size` le=300 → le=100，与 paginate 内部上限对齐 |
| 低危 | L-6 | Milvus 批删改循环分页（`_delete_by_expr_paged`），消除 16384 静默漏删 |
| 低危 | L-8 | `gen_batch_id` 序号取模，保持 5 位格式 |
| 低危 | L-9 | 去重数字归一前导零（"第04次" ≡ "第4次"） |
| 低危 | L-10 | `kb_cache` 同步 Redis 调用统一包 `asyncio.to_thread` |
| 低危 | L-11 | BM25 Level C 兜底结果不写 24h 缓存 |
| 低危 | L-12 | watchdog 需求回退脱离 `reclaimed>0` 分支，每轮幂等执行 |
| 低危 | L-13 | FP 进度分母固定为功能点总数，不再漂移 |
| P 项 | P-3 | `knowledge_qa` history / detail / delete / corrections / archive 补 current_user 与产品可见性过滤（越权读） |
| P 项 | P-5 | "业务场景"哨兵常量化 + 列表回显不再漏出哨兵值 |
| P 项 | P-6 | pytest 导出 `@pytest.mark.{type}` 净化（真正问题行是 190 而非 195） |
| P 项 | P-7 | `call()` docstring 返回值订正为四元组；循环变量遮蔽加注释说明 |
| P 项 | P-8 | `_update_task` 状态回写失败日志升级为 error 并带目标状态 |

### 未修复项及原因（后续跟进）

> v3.4.3（2026-09-18）：原暂缓项中的 **L-15、P-1 已修复**（见 §六），从本表移除。剩余 5 项：

| 条目 | 原因 | 建议 |
|------|------|------|
| L-14 | 断路器 provider 级统计 + HALF_OPEN 并发探测限制属熔断器重构，改动面与风险中等。**N-3 已大幅缓解**（403/401 硬失败立即禁用 600s、超时类累计 3 次禁用、单次调用 240s 总预算） | 单独立项，配套故障注入测试 |
| P-2 | 只哈希 FP id 属**有意设计**（以 id 为身份）；改名仅影响展示 | 不修 |
| P-4 | `approve_correction` 非幂等（向量先写、DB 后 commit）。按架构惯例（PG→Milvus 一律走出箱）应改走 `vector_outbox`，属架构改造 | 单独立项 |
| P-9 | watchdog 60 分钟阈值误杀"慢而活"的长 LLM 调用（`requirement_tasks.py` 全文无 `updated_at` 刷新），需加心跳。**N-3 后触发概率已降低** | 单独立项 |
| P-10 | 反代下共享限流桶。内网直连部署无影响；修复涉及部署层是否信任 `X-Forwarded-For`，与安全策略冲突（`auth.py` 明确拒绝信任该头） | 部署决策，非代码缺陷 |

> 每修复一项，请在该条目的"状态"改为 `已修复` 并在"修复记录"填写：修复 commit / 日期 / 验证方式。

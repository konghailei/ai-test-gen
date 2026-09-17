# KNOWN-ISSUES 独立复核报告（2026-09-17）

> 复核对象：`docs/KNOWN-ISSUES.md`（35 条已证实 + 11 条待确认）
> 复核方式：逐条源码通读 + 运行时取证（容器 `ai-test-gen-api` / `ai-test-gen-pg` 实测）
> 复核原则：不采信文档描述，一切以实际代码与运行时结果为准

---

## 一、总体结论

**文档主体可信，但不可直接作为修复工单使用。**

| 级别 | 总数 | 完全成立 | 部分成立（表述/影响面有偏差） | 不成立（误报） |
|------|------|---------|------------------------------|---------------|
| 🔴 高 | 5 | 5 | 0 | 0 |
| 🟡 中 | 15 | 9 | 4 | **2** |
| 🟢 低 | 15 | 12 | 3 | 0 |
| **合计** | **35** | **26** | **7** | **2** |

- 5 条高危**全部属实**，且 H-1/H-3/H-4 已取得运行时铁证（见 §3）。
- 2 条误报：**M-3、M-12**；7 条存在"机制真、描述错"的问题，若照原文修复会改错地方。
- 另有 **6 处严重程度定级偏差**（1 条应提级为高、1 条应提级为中、1 条应降级）。
- `P-11` 与 `L-11` 是同一问题的重复登记，"35 + 11"的计数口径虚增 1。

---

## 二、逐条判定

### 🔴 高危（5/5 成立）

| 编号 | 判定 | 证据与说明 |
|------|------|-----------|
| H-1 澄清失败回退成 draft | **成立** | `requirements.py:1257` 无条件置 draft（在 `if status=="confirmed"` 分支内）→ `:1396` 才快照 → `requirement_tasks.py:1698` 用该快照恢复。三段链路闭合，中间无 refresh/rollback（已 grep 确认）。`generate.py:137` 确认"仅 confirmed 可生成"，后果如文档所述。 |
| H-2 用例 ID 秒级时间戳撞主键 | **成立** | `testcases.py:276-277`。运行时佐证：库中真实存在 `TC-5148-771946`、`TC-5148-836749`（手工路径格式），与自动路径 `TC-ATTACHMENT-9243-6615` 格式明显不同，证明手工路径未走 `gen_test_case_id()`。 |
| H-3 向量化状态写不进库 | **成立（已运行时铁证）** | ORM 侧：`hasattr(TestCase,'vectorization_status') → False`，`TestCase.__table__` 向量列为空；赋值不报错但 `'vectorization_status' in TestCase.__mapper__.columns → False`。DB 侧：`information_schema` 中 `vector%` 列仅存在于 `requirements` 与 `functional_points`，`test_cases` **无**该列。结论：赋值必然静默丢弃。**补充**：文档只对比了 Requirement，实际 `functional_points` 也有该列——三张表里只有 test_cases 缺，更印证是遗漏而非设计。 |
| H-4 互斥锁在两次 commit 间释放 | **成立** | `_check_no_running_task` 用 `pg_advisory_xact_lock`（`requirements.py:2732`，事务级锁，commit 即释放）。`submit_clarify`：拿锁 → `:1398` commit（释放）→ `:1414` 再 commit 插任务。`regenerate`：`:1879` 拿锁 → `:1883` commit → `:1899` commit。窗口真实存在。 |
| H-5 fire-and-forget 无强引用 | **成立** | `requirements.py:3721` 裸 `asyncio.create_task(`，无变量接收；`task_selfheal.py:193` 裸 `asyncio.get_running_loop().create_task(`。同文件 `requirements.py:107` 的 `_create_bg_task`（带 done_callback 强引用）确实未被这两处使用。 |

### 🟡 中危（9 成立 / 4 部分 / 2 误报）

| 编号 | 判定 | 关键偏差 |
|------|------|---------|
| M-1 rerank 硬截 5 条 | 成立 | 机制属实（`hybrid_search.py:113-118/125-131`，`MATCH_FINAL_N=5` 默认、`RERANKER_ENABLED=True` 默认）。**但当前无实弹**：仅有的两个调用点 `knowledge_qa.py:617`、`clarify_kb.py:144` 都传 `top_k=5`。属潜在缺陷（`search_requirements` 默认 `top_k=20`，脚本调用会踩）。 |
| M-2 KB 缓存失效点缺失 | 成立（后果部分夸大） | TTL=86400、全项目仅 `products.py:986/1525` 两处失效——属实。但"已删除需求还在"在答疑路径被 `_enrich_sources`（回查 PG 丢弃孤儿）缓解；只有 ClarifyAgent（`clarify_kb.py:156` 直接用 matched）不校验。"新归档搜不到"需同 query 命中旧缓存才发生。 |
| **M-3 BM25 缺 doc_type/project_id** | **不成立（方向反了）** | ① `_bm25_search` **已接收并使用 product_id**，SQL 内含 `product_id = :product_id` 过滤（`:187`），"其他项目数据穿透"错误；② `requirements` 表**无 doc_type 列**（运行时确认 `[]`），FP 根本不在该表，BM25 是 `FROM requirements`，不可能"混入 FP"。真实情况恰好相反：降级到 BM25 时是**丢失 FP 召回**，不是混入。 |
| M-4 令牌桶放行速率超配 | **成立（已实测）** | 算法属实但文档**措辞错误**：`last_refill = now - (elapsed % window)` 在 `elapsed < window` 时恒等于旧值，是空操作；真正根因是 `last_refill` 从不推进，`refill` 每次从同一原点重复累计。**实测**（`limit=5/window=2s`，1 秒内 20 次请求）：现行实现放行 **16** 次，将 `last_refill` 正确推进后放行 **3** 次，理论值 2~3 次 —— 实际放行速率约为配置的 **5 倍**。建议**提级为 🔴**。 |
| M-5 日志 UTC 边界 | 成立（描述不全） | `logs.py:34/45-47` 硬编码 `.replace(tzinfo=utc)`，字段为 TIMESTAMPTZ（UTC）。除文档所述"漏掉本地 00:00~08:00"，还会**误含次日凌晨 00:00~08:00** 的日志。 |
| M-6 go_live_date naive | **部分成立** | 三处中 **`products.py:1404` 已修复**（`:1413-1414` 补 `replace(tzinfo=utc)`）；仅 `products.py:687`、`requirements.py:2332` 两处真实存在。 |
| M-7 归档无幂等 | 成立 | `kqa_archive` 全程无 `record.archived` 判断；每次 `gen_requirement_id()` 新 ID、无唯一约束冲突，重复提交必产生第二条 archived 记录并覆盖 `archived_id`。 |
| M-8 请求级 session 进 SSE | 成立（措辞偏差） | 确实传入请求级 session 对象并持有到流尾 `:562-563` commit，连接池（pool 10+overflow 10）耗尽风险成立。但生成器**中途未再使用**该 session（仅开头 `_enrich_sources` 与结尾 commit），且 LLM 调用是**非流式一次性**（`:513-521` 注释已写明），非"流式循环中反复查询"。 |
| M-9 批量删除只抽查一条 | 成立（影响面更大） | 除文档所述外：① `:457 if probe_pid:` 为假（product_id 为空/查不到）时**整个校验被跳过**；② 删除范围含 `Traceability`、`ErrorCase` 级联与 Milvus 向量（`:461-476`），误删波及面大于"删用例"。 |
| M-10 批量审核静默忽略 | **部分成立** | 成立部分：不存在的 ID 静默忽略、无 `not_found` 字段、message 仍宣称全部成功。**不成立部分**：文档称"不属于当前产品的 case_id 被静默忽略"——实际 `:582-583` 会 fail-fast 抛 403 整体拒绝。两种情形被混为一谈。 |
| M-11 Celery 降级死代码 | 成立（附带描述有误） | `celery_app is None` 仅 ImportError 可达（`:55-57`），而 celery 是硬依赖 → 降级分支不可达，属实。但"必然连接失败/进 DLQ"不准确：兜底 `redis://localhost:6379/1` 若本机有 Redis 则**静默入队无人消费**（更难排查）；且发布阶段失败时任务根本不执行，不会写 `pending_retry/failed`。 |
| **M-12 全局 180 与任务级 240 冲突** | **不成立** | Celery 超时优先级为「调用级 > 任务属性 > 全局配置」。任务已显式声明 `time_limit=240/soft=210`（`embedding_tasks.py:78-79`），全局 180 对其**不生效**，不存在 SIGKILL 冲突。标题本身即为误报。 |
| M-13 prompt get 同步往返 | 成立（**显著低估**） | 文档称"2~3 次往返"；实测代码路径：单次 `get()` = 14 次 `register_default`（各含 `_ensure_table` + SELECT + **无条件 COMMIT**）+ `get_prompt` + `get_prompt_default`，即约 **16 次 information_schema 查询 + 16 次 SELECT + 14 次 COMMIT**，全部同步阻塞在事件循环内，且**每次 LLM 调用前都发生**。建议**提级为 🔴**。 |
| M-14 缓存 key 缺字段 | 部分成立 | key 字段缺失属实。但需"加工后 prompt 逐字节相同 + 双方显式 `temperature=0`"（`call` 默认 0.3、`call_json` 默认 0.2），当前 `temperature=0` 的两处 `agent_name` 不同，**无实际碰撞点**。另文档未提：命中缓存时返回空 usage/model/provider，会污染调用日志。 |
| M-15 缺 overall_score 当 0 分 | 部分成立 | 代码行为属实（`:200-207`），但触发路径不成立：`quality_gate.check` 唯一调用点的入参恒含 `overall_score`（`clarify_score.py:272-281`），缺键的半残 report（`clarify_score.py:170-176`）流向的是 `_clarify_state`，**不经过 gate**。属不可达的防御性缺陷。 |

### 🟢 低危（12 成立 / 3 部分）

| 编号 | 判定 | 关键偏差 |
|------|------|---------|
| L-1 Markdown 表格换行 | 成立 | 行号准确，无 `\n` 处理。补充：`:164` 标题行同样未处理。 |
| L-2 Gherkin 换行 | 成立 | 行号准确，全文件零清洗。 |
| L-3 XMind 控制字符 | 成立 | 行号准确，`xmind.py`/`req_xmind.py` 均无 sanitize（全目录仅 `pytest_code.py` 有）。 |
| L-4 datetime.utcnow | 成立 | 行号准确。 |
| L-5 page_size 静默钳制 | 成立（**定位需修正**） | 钳制属实且响应回显请求值。但"实弹"端点是 `requirements.py:777`（`le=300` 与内部上限 100 不一致）；`testcases.py:71` 等其余端点 `le=100` 会在 FastAPI 层先返 422，钳制对它们不可达。 |
| L-6 Milvus 16384 上限 | 成立 | 四处结构一致，无分页循环，`return len(ids)` 被当作全删数。建议提为 🟡（静默漏删无告警）。 |
| L-7 编辑硬置 reviewed | **部分成立** | 实为**有条件**：仅当 `updated_fields` 命中 `content_fields` 时置 reviewed（`:412-415`），拖拽移动不改状态。但确实不检查当前 status，approved 降级/rejected 跳过驳回的后果成立。 |
| L-8 gen_batch_id 溢出 | 成立（概率低估） | `_batch_counter` 初值随机 `0~99999`，溢出所需次数 = `99999 - 初值`，**期望约 5 万次、最坏 1 次即溢出**，并非"一天 10 万批次"。运行数据中已见 `BATCH-20260915-75765` 等高位值。 |
| L-9 前导零不归一 | 成立 | 推演：`"04"` vs `"4"` 数字集合不等 → 数字护栏直接短路返回 False，n-gram 相似度轮不到计算。 |
| L-10 同步 Redis 阻塞 | 成立 | 导入同步 `redis`，`get/set/invalidate` 均为 async 函数内裸调用，无 `to_thread`。建议提为 🟡。 |
| L-11 Level-C 进缓存 | 成立 | `cacheable` 只看结果非空、不分 level。 |
| L-12 watchdog 回退条件 | **部分成立** | 前半成立："回退被包在 `if reclaimed:` 内，`reclaimed==0` 不回退"属实。**后半不成立**：任务 UPDATE 与需求 UPDATE 在**同一事务**（唯一 commit 在 `:91`），失败时整体回滚，任务仍为 running，下轮巡检自动重试——不存在"任务已中断、需求无补偿"。 |
| L-13 FP 进度分母漂移 | 成立（更严重） | `total` 取"已产生 subtask 的 FP 数"而非 `len(fp_list)`（真值就在 `:1058` 作用域内未用）；失败 FP 计入 total 却永不进 covered，进度到不了 100%；`__integration__` 被排除出 total 却计入 covered。 |
| L-14 断路器粒度 | 成立 | `record_failure()` 全函数仅一处、在 provider 循环之后；HALF_OPEN 直接 `return True` 无并发探测上限。缓解：provider 级连续失败 3 次禁用 600s 仍在。 |
| L-15 流式误判过宽 | **部分成立** | 匹配过宽属实（`"stream"` + `support/invalid/unknown/not found`，瞬时链路错误会被误判）。但"**永久降级**"不成立：全仓库无 `stream_supported` 类持久标记，回退仅作用于本次调用。 |

### P 系列（待确认项复核）

| 编号 | 判定 | 说明 |
|------|------|------|
| P-1 指纹 product_id 两侧来源不一致 | 成立 | checkpoint 侧取 `srr.requirement.product_id`（`orchestrator.py:1072`），校验侧取 DB 字段（`pipeline_fingerprint.py:170`）。正常路径一致，但旧数据 / product_id 变更 / 调用方未传值时会分叉并**每次续跑必 409**。建议 🟡。 |
| P-2 fp_set_sha256 只哈希 id | 成立 | 哈希输入仅 id。但注释表明是**有意设计**（以 id 为身份），改名失真仅影响展示。🟢 |
| P-3 问答历史/详情/corrections 缺可见性过滤 | **成立（比描述更宽）** | 三个端点连 `current_user` 都未注入，全文件无 `assert_product_visible` 调用；`history`/`detail` 返回**全产品**数据。属**越权读**，同文件写端点有权限校验、读端点漏做。建议**提级为 🔴**。 |
| P-4 approve_correction 非幂等 | 成立 | 向量先写、DB 状态靠请求尾 commit；独立片段分支每次新 ID，重试产生重复孤儿向量。建议 🟡。 |
| P-5 `_extract_fp` 魔法字符串 | 部分成立 | `_extract_fp`（`:46-50`）本身**不含**过滤逻辑，魔法字符串在调用点 `:128`；且 `:169` 未做同样过滤，会把哨兵值 `"业务场景"` 原样回显。无常量定义。 |
| P-6 pytest type 未净化 | 成立（**行号错**） | 真正的问题行是 **`:190` `f"@pytest.mark.{typ}"`**；文档所指 `:195` 恰是 tag 的 `re.sub` 正确净化行。 |
| P-7 变量遮蔽 / docstring 不符 | 部分成立 | 遮蔽属实（`:347` 循环变量覆盖 `:268` 参数，当前无功能影响）；返回值不符在 **`:296-297`**（docstring 写 3 元、实际 4 元），而 `:901` 的问题是**函数注解**与实际返回不符，归因不准。 |
| P-8 `_update_task` 吞异常 | 成立 | 函数整体被 `except Exception` 包裹，只记 warning、不 raise。建议 🟡。 |
| P-9 watchdog 60 分钟误杀 | 成立 | 判定字段仅 `updated_at`，全仓库无独立心跳；长 LLM 段 >60 分钟会被标 interrupted，且因 `status != "interrupted"` 守卫**无法写回**，任务永久中断。建议 🟡。 |
| P-10 反代共享限流桶 | 成立 | 用 `request.client.host`；全仓库无 XFF 解析，`auth.py:164-165` 注释明确拒绝信任该头，无缓解措施。建议 🟡。 |
| P-11 | **重复条目** | 与 L-11 同一问题，应删除或改为交叉引用。 |

---

## 三、运行时取证记录（可复现）

```bash
# H-3：ORM 侧（输出 False / [] / True / False）
docker exec ai-test-gen-api python -c "
from app.db.models import TestCase, Requirement
print(hasattr(TestCase,'vectorization_status'))
print([c.name for c in TestCase.__table__.columns if 'vector' in c.name])
tc=TestCase(id='PROBE-1'); tc.vectorization_status='completed'
print('vectorization_status' in tc.__dict__, 'vectorization_status' in TestCase.__mapper__.columns)"

# H-3：DB 侧（test_cases 无向量化列；它只存在于 requirements / functional_points）
docker exec -i ai-test-gen-api python - <<'EOF'
import asyncio
from sqlalchemy import text
from app.db.database import async_session
async def m():
    async with async_session() as s:
        r=await s.execute(text("select table_name,column_name from information_schema.columns where column_name like 'vector%' order by 1"))
        print(r.fetchall())
asyncio.run(m())
EOF

# H-2：库里真实的手工创建 ID（TC-<req后缀>-<6位秒时间戳>）
#   -> TC-5148-771946 / TC-5148-836749   vs 自动路径 TC-ATTACHMENT-9243-6615
# M-4：令牌桶实测 -> 现行 16/20 放行，修正实现 3/20（理论 2~3）
```

---

## 四、修复优先级建议（在文档原定级基础上调整）

| 优先级 | 条目 | 理由 |
|--------|------|------|
| P0 立即 | **H-3** | 静默数据丢失，前端永远显示幽灵 pending，且修复只需加两列 |
| P0 立即 | **H-1 / H-2 / H-4 / H-5** | 状态错乱、主键冲突、并发双建、任务静默丢失 |
| P0 立即 | **M-4**（提级） | 限流保护实际放行 5 倍流量，防护形同虚设，改动极小 |
| P1 高 | **M-13**（提级） | 每次 LLM 调用前 16 次同步 DB 往返阻塞事件循环，性能与稳定性风险 |
| P1 高 | **P-3**（提级） | 越权读其他产品数据，同文件写端点有校验、读端点漏做 |
| P1 高 | H 修复后 → **M-9 / M-7 / M-8** | 误删 / 重复归档 / 连接池耗尽，均有实际触发路径 |
| P2 中 | M-1 / M-2 / M-5 / M-6 / M-10 / M-11 / L-6 / L-10 / P-1 / P-4 / P-8 / P-9 / P-10 | 触发面窄或影响可观测性 |
| P3 低 | 其余 L 系列与 P-2 / P-5~P-7 | 展示类 / 卫生类 |

### 需先订正文档再修复的 7 条

| 编号 | 订正内容 |
|------|---------|
| **M-3** | 判定为不成立，应移出修复清单；若确有召回需求，按"BM25 降级丢失 FP 召回"重新立项 |
| **M-12** | 判定为不成立，直接关闭 |
| M-6 | 三处改两处（`products.py:1404` 已修复） |
| M-10 | 区分"不存在 ID"（静默）与"不可见 ID"（403 整体拒绝） |
| L-5 | 实弹端点改为 `requirements.py`（`le=300`） |
| L-8 | 触发概率改为"期望约 5 万次调用、最坏 1 次" |
| P-5 / P-6 / P-7 | 修正定位：`_extract_fp` 不含过滤（调用点 `:128`）；P-6 行号 `190`；P-7 归因到 `:296-297` |

---

## 五、复核局限

- 未对全部条目做端到端运行时复现（H-1/H-2/H-4/H-5、M-2~M-15 及 L/P 系列以静态链路确证为主）；
- M-4、H-2、H-3 已取得运行时确定性证据，其中 M-4 与 H-3 复现成本极低、结论可靠；
- L-6 的 16384 上限未构造超量数据实测，依据为代码硬编码值与 Milvus query 默认上限；
- 未发现文档存在"凭空捏造"的条目——2 条误报均为**推理方向或 Celery 语义判断错误**，而非虚构代码。

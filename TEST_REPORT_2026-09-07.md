# 系统测试报告（2026-09-07）

> 测试环境：容器化真实环境（ai-test-gen-api v3.1.2 + PG + Milvus + Redis + Celery，均 healthy）
> 测试方式：真实 API 冒烟/容错/并发 + 库内 1266 条用例质量抽样 + 三个专项静态代码审查 + 容器内回归套件
> 遵循 TEST_PLAN.md 原则：P3 阻塞即修（本轮修复 1 项 P0）、其余记录待确认

---

## 一、结论速览

| 维度 | 结果 |
| --- | --- |
| 功能性（真实 API） | ✅ 25/25 通过（登录/列表/4 种格式导出/权限边界） |
| 容错（异常输入） | ✅ 8/8 通过（异常输入均 4xx，无 500 泄漏） |
| 性能 | ⚠️ 读接口 P95=416ms 达标；但发现**间歇性整体卡顿 10~20s**（见 §4） |
| 生成内容质量 | ⚠️ 完整性 100%、多样性好；置信度区分度不足 + 1 处枚举越界（见 §5） |
| 代码审查 | ❌ 发现 1 项 P0（已修）+ 3 项 P1 + 多项 P2（见 §6） |
| 回归套件 | phase5 101/101 ✅、phase6 141/141 ✅、phase4 修复后 137/141（详见 §7） |

---

## 二、功能性测试（真实 API，容器内执行）

冒烟脚本：`backend/_e2e_tmp/smoke_test.py`（25 项断言全过，报告 JSON 同目录）

- admin 登录 / 产品列表 / 需求列表（total=20）/ 用例列表（**total=1266**）全部正常
- 导出真实数据 20 条用例：xmind / xmind8 / markdown / json 四种格式全部 200 且文件有效
- 导出 20 条用例耗时 20ms

## 三、权限边界（测试计划阶段 1）

- test_qa_01 / test_user_01（user 角色）独立登录 ✅，访问 `/admin/users` 均正确 403 ✅
- admin 正常 200 ✅

⚠️ 但注意 §6-2：**产品级数据隔离在代码层缺失**，当前 403 只挡了 admin 接口，普通用户之间、跨产品之间无隔离（IDOR 风险）。

## 四、性能

**达标项**：20 并发读接口成功率 100%，平均 237ms，P95 416ms。

**发现的问题**：
1. **间歇性服务整体卡顿（P1）**：测试中两次观测到所有接口同时超时（health 单次 20s 超时、9.9s 慢响应），数十秒后自行恢复。结合静态审查发现 `gen_agent.py:158` 在 async 事件循环内**同步调用** Milvus 检索（未 `asyncio.to_thread`），高度怀疑是生成/检索任务阻塞了唯一的事件循环。单 worker + 同步阻塞的组合会让一个慢调用冻结整个服务。
2. **`--reload` 在容器中开启（P2）**：向挂载目录写任何文件都会触发服务重启，重启窗口内全部请求超时。测试环境如此、若生产 compose 同款配置则为可用性隐患。

## 五、生成内容质量（抽样 300/1266 条）

| 指标 | 结果 | 评价 |
| --- | --- | --- |
| 字段完整率 | title/type/priority/description/preconditions/steps 全部 **100%** | ✅ |
| 步骤含预期结果 | 705/705 = **100%**，空操作 0 | ✅ |
| 类型多样性 | 8 种类型（functional 89 / regression 81 / exception 48 / boundary 40 / security 24 / performance 8 / integration 7 / exploratory 3） | ✅ |
| 优先级分布 | P1 152 / P2 82 / P0 63 / P3 3 | ✅ 合理 |
| 重复标题 | 仅 2 组（0.7%） | ✅ |
| **置信度分布** | **300 条全部 ≥0.8，平均 0.95** | ⚠️ 区分度不足 |
| 状态分布 | approved 297 / reviewed 3 | 正常 |

两个问题：
1. **置信度无区分度（P2）**：全部 ≥0.8、平均 0.95，自评分数趋同于高分，无法起到"低置信度分流人工审核"的作用（G8 目标依赖该机制）。建议校准评分 prompt 或引入外部校验信号。
2. **类型枚举越界（P2）**：生成器可产出 `exploratory`（后端 UTCF/prompt 允许），但前端 `labels.ts`、`XMindEditor`、`XMindEditPage` 及后端 `exporters/xmind.py` 的类型表都**没有** exploratory —— 显示为原文、XMind 导出排到"未知"末尾。前后端类型枚举需对齐（或加"探索"映射）。

## 六、代码审查发现的 Bug / 逻辑漏洞

### 已修复（本轮，P0）

**P0-1 生成流水线错误被静默吞掉**（`core/orchestrator.py` run_pipeline，已修复并回归验证）
v5.0 事件队列改造后，节点异常（如 SRR 为空的 ValueError）发生时 `_producer` 协程直接死亡、只投递 `None` 哨兵；消费循环误判"正常结束"后**继续执行"流水线完成"分支，向前端 yield `done` 事件**。后果：用户看到"生成完成"但 0 条用例，无任何错误提示，DLQ 也不记录，事后无法审计。回归套件 phase4 当场崩溃证实了此问题。
修复：producer 捕获异常后把异常对象放入队列，消费循环识别后重新抛出，交由外层 except 统一走 error 事件 + DLQ。修复后 phase4 由崩溃恢复为 137/141，phase5 SSE 错误路径 101/101 复验通过。

### 待修复清单

**P1（严重）**
1. **多产品数据隔离缺失（IDOR）**：`requirements.py:2011`、`testcases.py:74`、`export.py:132` 仅在客户端传入 `product_id` 时过滤；`get/update/delete_testcase`、`feedback` 只按 ID 操作、无产品归属校验，`Product.visible_to_user_ids` 全系统无人校验。已知 case_id 即可跨产品读写删。
2. **并发生成无 running 任务互斥**：`generate.py:189-201` 直接建 running 任务，未像 clarify/impact 那样调用 `_check_no_running_task`，并发触发会重复生成 + 幽灵任务。
3. **删除需求漏删 `ErrorCase`**（`requirements.py:4223`）：`error_cases.original_case_id` 外键无 ondelete，删除含低置信度用例的需求会 IntegrityError 500（对比 `testcases.py:432` 批量删除已有正确处理）。
4. **PG/Milvus 双写不一致被吞**：`testcases.py:559-583/667-689`、`review_agent.py:782-800` 审核入库后写 Milvus 失败仅 warning，不标记 `vectorization_status`、不重试 → 回归复用基线静默缺失。

**P2（一般）**
5. 重新生成不清旧批次用例 → 同需求用例跨批次累积重复（`review_agent.py:658`）。
6. `update_testcase` 改 `req_id` 不同步 `product_id`、不校验目标需求存在（`testcases.py:356`）。
7. LLM 响应缓存 `_response_cache` 无租户隔离与 TTL（`llm_client.py:220`），相同 prompt 跨需求串味。
8. 事件循环阻塞（见 §4-1）与 embedding 重复计算（`gen_agent.py:886/964` 同批标题编码 2-3 次）。
9. Milvus 调用无超时 + 每次检索重复 `collection.load()`（`milvus_store.py:308`）。
10. `TestCases.tsx:80` 前端 5000 条硬截断且 total 失真；`:116` 加载失败静默显示空态（易用性，误导"没有数据"）。
11. XMind 导图 Delete 键删节点无确认无撤销（`XMindEditPage.tsx:675`），叠加"保存即删库"语义，误删风险高。
12. 保存部分失败（failCount>0）时不刷新数据，前端与库不一致且仅有一行提示（`XMindEditPage.tsx:581`）。

## 七、回归套件（容器内实跑）

| 套件 | 基线 | 本轮 | 说明 |
| --- | --- | --- | --- |
| phase4 orchestrator | 141/141 | **137/141**（修复前：崩溃） | P0 修复后不再崩溃；4 失败中 2 项为**测试期望过时**（v3.x 有意设计：confirmed 需求跳过 clarify，`orchestrator.py:1330-1348` 有注释为证），2 项（gen 收到 historical_regression/coverage_gaps）**待查**——需要确认 v3.1.2 的 FP 分片路径参数传递或同步测试 |
| phase5 API | 100/100 | **101/101** ✅ | 含 SSE 错误路径，P0 修复无副作用 |
| phase6 exporters | 141/141 | **141/141** ✅ | |

## 八、后续建议（按优先级）

1. 产品数据隔离补全（P1-1）——唯一的安全类问题，建议最优先
2. 事件循环同步阻塞改造（P2-8）——解释并消除间歇性卡顿
3. 生成入口加 running 任务互斥（P1-2）+ 删除需求补删 ErrorCase（P1-3）
4. 置信度校准 + 前后端类型枚举对齐（含 exploratory）
5. phase4 剩余 2 项失败根因确认（区分真回归/测试过时）
6. 前端：5000 条截断、空态误导、Delete 无确认

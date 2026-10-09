# docs/23 — LLM 供应商禁用机制改造 + 等待重试（v3.5.6）

> 状态：**已实施**（2026-10-09）。大哥拍板方案，见 §0 决策记录。
> 起因：2026-10-09 自举 E2E 实证「需求文档混合上传」FP 5 个子批全部 0 条产出，
> 根因 = 三家供应商同时落入内存禁用窗口（软失败累积 3 次 → 600s 禁用）。

## 0. 大哥拍板的 4 项决策

| # | 决策 | 说明 |
|---|---|---|
| 1 | 首响应超时：首轮 180s / 重试轮 60s | 避免误杀"prefill 慢但能出结果"的慢模型；重试轮前提是"刚失败过"，60s 无输出即可判未恢复 |
| 2 | 通知形式：顶部导航铃铛 + 独立通知页面 | 未读数角标 + 列表页，可标记已读 |
| 3 | 主供应商被禁用 → 主标记自动让位 | 让给下一个启用中的供应商（全局唯一主标记） |
| 4 | 硬失败与全败**都**通知 | 便于及时排查 |

## 1. 旧机制的问题（实证）

| 问题 | 表现 |
|---|---|
| **软失败跨调用禁用** | 超时/5xx 累积 3 次 → 内存禁用 600s；自动恢复后又反复撞同一家慢供应商 |
| **禁用状态在内存** | `_provider_disabled_until` 进程内 dict → 重启即失效、web/worker 多进程不一致 |
| **全禁用即零重试失败** | 三家同时进禁用窗口 → `LLMNoProviderAvailable` 立即抛出，整个 FP 的子批全灭 |
| **运维不可见** | 禁用发生在日志里，前端/设置页完全看不到，无从排查 |
| **硬失败自动恢复** | 403 额度耗尽禁用 600s 后自动恢复 → 又去撞，白等首响应超时 |

## 2. 新机制设计

### 2.1 失败分类与处置

| 类型 | 例子 | 处置 |
|---|---|---|
| **软失败** | 超时、5xx、重试耗尽 | 仅**本次 call 内**让位给下一家；**不再跨调用禁用**。全部失败 → 等待重试 3 轮 |
| **硬失败** | 401/403、额度耗尽、模型不存在 | **持久化禁用**（DB `enabled=false`，不自动恢复）+ 写管理员通知 + 主标记让位；管理员手动启用 |

### 2.2 等待重试（`call()` 外层循环）

```
第 1 轮：遍历启用中的候选（首响应超时 180s）
  全败 → SSE 推「供应商暂不可用，60s 后自动重试（1/2）」标红 → 等待 60s
        （等待期按 COUNTDOWN_TICK=5s 分片推送倒计时，前端逐秒递减显示）
第 2 轮：重新构建候选（硬失败家已被剔除），首响应超时收紧 60s
  全败 → 同上提示（2/2）→ 等待 60s（含倒计时）
第 3 轮：同第 2 轮
  全败 → 写管理员通知 + 抛出（前端红色 error 提示人工排查）
特例：候选为空（全被持久化禁用）→ 不等待，立即失败（等待无意义）
```

单次 call 最坏耗时 ≈ 3 轮 × 3 家 ×（180s 或 60s）+ 2×60s 等待 ≈ 11-15 分钟（可控）。

**等待期倒计时**（v3.5.6 补充）：`_emit_wait_countdown` 按 `COUNTDOWN_TICK=5s` 分片
推送 `wait_retry.level='countdown'` + `remain_sec`，前端收到后用本地 1s 定时器逐秒递减，
视觉上连续；`done`/`error` 时清定时器。

### 2.3 数据模型（v3.5.6 新增）

`llm_config` 新增 4 列（`SCHEMA_MIGRATIONS` 自动补列）：

| 列 | 说明 |
|---|---|
| `enabled` | BOOLEAN DEFAULT TRUE —— 总开关（与 `is_primary` 正交：后者是主备优先级） |
| `disabled_reason` | 人工禁用固定"人工禁用"；系统禁用写具体硬失败原因 |
| `disabled_by` | `manual` / `system` / 空 |
| `disabled_at` | 禁用时间 |

新表 `admin_notifications`：`id/type/title/content/related_type/related_id/is_read/created_at`。

### 2.4 常量变更

| 常量 | 旧 | 新 |
|---|---|---|
| `MAX_ATTEMPTS` | 5 | **3**（429 限流单独计数，不计入） |
| `PROVIDER_FAILURE_THRESHOLD` | 3（触发禁用） | 3（仅调用内计数） |
| `PROVIDER_DISABLE_SECONDS` | 600（自动恢复） | 兼容保留，新逻辑不再使用 |
| `WAIT_RETRY_INTERVAL` | — | **60**（新增） |
| `RETRY_ROUNDS` | — | **2**（新增，总 3 轮） |
| `DEFAULT_RETRY_FIRST_RESPONSE_TIMEOUT` | — | **60**（新增，重试轮收紧） |

## 3. 改动清单

| 文件 | 改动 |
|---|---|
| `db/models.py` | LLMConfig 加 4 列；新增 `AdminNotification` |
| `db/database.py` | `SCHEMA_MIGRATIONS["llm_config"]` 补 4 列；导入新模型 |
| `core/provider_admin.py` | **新建**：`disable_provider_persistently`（禁用+通知+让位）、`set_provider_enabled`（手动启停）、`notify_all_providers_failed`（全败通知） |
| `core/llm_client.py` | 候选过滤加 DB 禁用判断；`_record_provider_failure` 不再跨调用禁用；`_disable_provider_now` 改为持久化（async）；`call()` 加等待重试外层循环 + SSE 回调钩子；`_call_with_retry` 加 `first_response_timeout` 覆写 |
| `core/orchestrator.py` | 生成任务注入 `_wait_retry_cb`（推 SSE `wait_retry` 事件），finally 重置 |
| `api/settings.py` | 加载时重建禁用缓存 + 跳过禁用家；`_build_item` 返回禁用字段；新增 `PUT /llm/{name}/enabled`、`GET/POST /notifications[/read]` |
| `models/api_schemas.py` | `LLMConfigItem` 加 4 字段 |
| `frontend` | types 加字段；client 加 4 个接口；Settings 加启停开关+禁用原因；**新建 Notifications.tsx** + 路由；Layout 加铃铛未读数（60s 轮询）；Generate 加等待重试红色提示条 |

## 4. 验证结果

| 项 | 结果 |
|---|---|
| `test_provider_disable_retry.py`（新建 9 项） | **9/9 全绿** |
| 守卫测试子集（并发/禁用/去重/发布/D-5/D-6） | **117 passed** |
| phase4 回归（脚本式） | **186/186** |
| phase5 回归（脚本式） | **112/112** |
| 新端点全链路探针 | GET /llm 含 enabled ✓ / 手动禁用→reason"人工禁用"by=manual ✓ / 通知列表+已读 ✓ / 禁用缓存重建 ✓ |
| `tsc --noEmit` + `npm run build` | 零错误，构建成功 |

## 5. 排查记录：改常量引入的真 bug（重要）

`call_vision`（多模态链路）原用 `or self.DEFAULT_CALL_TOTAL_BUDGET` 取预算——
常量从 240 改 0 后，`deadline = 开始时间 + 0` → **多模态调用全部秒挂**。
**教训：改"声明常量"必须全量 grep 消费方**（本次 grep 补救抓到，两处已同步修复）。

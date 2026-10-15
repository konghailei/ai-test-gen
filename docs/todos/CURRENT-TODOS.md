# 当前有效待办清单（CURRENT-TODOS）

> 更新日期：2026-10-15 ｜ 系统版本：v4.0.2
> 维护纪律见 `todos/README.md`。**每条待办均经 git log + 代码双重核实，非文档快照照抄。**

---

## 一、运营/决策类（需大哥拍板或提供条件）

| # | 事项 | 来源 | 状态 | 关闭条件 |
|---|---|---|---|---|
| 1 | **DEDUP_EMBED_WORKERS 服务器 bench 定档**：dev 机默认串行，生产具体值需实测后定 | docs/30 §8.1 第 7 项 | ⬜ 待办 | 服务器跑一轮 bench，按 CPU/内存余量定值写入配置 |
| 2 | **真实生成 e2e 回验**（v3.5.9 提出）：验 SSE 进度 + 主合并点命中率日志 | docs/30 §8.1 第 6 项 | ⬜ 待办（v3.5.10/v4.0.x 已做多轮全量回归+自证，**此条是否已被覆盖待大哥确认**，确认后可直接关闭） | 提供需求跑一轮，核对 SSE 进度事件与合并点日志 |
| 3 | **语义去重评测重建**（P1）：per-req 域 + 真标签集 + 比 PR/F1 而非固定阈值 | docs/30 §8.1 第 2 项 | ⏸ 暂缓（docs/32 §4 理由） | 重启条件（满足其一）：B-6 落地后误删率抽检仍 >5%；或评估 bge-base/ONNX 量化等表征层变更；或阈值需重定标 |

## 二、技术债类（已定位、单独立项、未排期）

| # | 事项 | 来源 | 状态 | 说明 |
|---|---|---|---|---|
| 4 | **L-14 断路器 provider 级重构**：record_failure 全函数仅一处且在 provider 循环之后；HALF_OPEN 直接 return True 无并发探测上限 | KNOWN-ISSUES L-14 | ⬜ 暂缓 | 缓解仍在：provider 级连续失败 3 次禁用 600s |
| 5 | **P-4 approve_correction 非幂等**：向量先写、DB 状态靠请求尾 commit；独立片段分支重试产生重复孤儿向量 | KNOWN-ISSUES P-4 | ⬜ 待立项 | 建议 🟡 |
| 6 | **P-9 watchdog 无独立心跳**：判定仅看 updated_at，长 LLM 段 >60 分钟会被误标 interrupted 且因 status 守卫无法写回，任务永久中断 | KNOWN-ISSUES P-9 | ⬜ 待立项 | 建议 🟡 |
| 7 | **P-10 反代共享限流桶**：用 request.client.host，无 XFF 解析（auth.py 明确拒绝信任该头） | KNOWN-ISSUES P-10 | ⬜ 部署决策 | 内网单机部署可接受；上反代需定方案 |
| 8 | **P-2 fp_set_sha256 只哈希 id** | KNOWN-ISSUES P-2 | ⛔ 不修复 | 有意设计（以 id 为身份），仅展示层影响 |

## 三、已关闭（2026-10-15 复核划掉，留痕）

| # | 事项 | 关闭依据 |
|---|---|---|
| — | docs/32 全部 B-1~B-6 + O-1~O-4（曾误判为"定稿待实施"） | v3.5.10 已实施：`df14638`（B-1/B-2/O-2）、`dc1a1e9`（B-3/B-4/B-5）、`6b2f870`（B-6/O-1）；TEST_PLAN v5.1 引用其为验收基线 |
| — | N4 需求解析可永久挂起 | `da9847e` 任务看门狗 + analyzing 联动回退 |
| — | N3 澄清问题跨来源重复（实测 42.9%） | v2.8.68 跨来源去重（clarify_agent.py:872） |
| — | R3 同批次标题级重复 | review_agent.py:1304 入库前标题级严格去重 |
| — | N2「V1.1 需求文档自相矛盾」 | `0477a9c`（2026-09-11 16:03）已修；且该文档是 dogfood 测试素材（tests/backend/tests 下博客创作平台文档），非系统缺陷 |
| — | 循环权威风险（答案依据 AI 生成用例） | docs/14 N1 一期治理 2026-09-11 落地：source 分级（review_agent.py `_resolve_case_source`）+ 检索块重写 + RetrievalThreshold 标定 |
| — | docs/30 §8.1 第 1 项（P0 语义去重误删） | v3.5.10 B-6 三级闸门 |
| — | docs/30 §8.1 第 4 项（base 镜像重建） | docs/31 实测闭环 |
| — | docs/30 §8.1 第 5 项（缺陷 E 跨子轮重复） | v3.4.5 跨子轮语义去重 |
| — | docs/30 §8.1 第 8 项（待 push commit） | v3.5.10 tag 已在远程（已 push） |

## 四、文档状态勘误记录（防误导台账）

| 文档 | 问题 | 处置 |
|---|---|---|
| `.trae/specs/spec-20260901-full-cycle-test/tasks.md` | 2026-09-11 回填快照，N2/N3/N4/R3 等条目当天已修复未回写 | 未移动（.trae 为 IDE 规范目录）；引用前必须 git log 核查 |
| `docs/32-验证缺陷修复定稿方案.md` | 头部原写"定稿待实施"，实际 v3.5.10 已全部实施 | 已在头部补记实施状态（2026-10-15） |
| `docs/30 §8.1` | 第 8 项未划掉 | 已划掉并注明 |
| `tests/博客创作平台-*.md`（根目录 2 份） | 与 backend/tests/ 活引用副本完全重复（md5 一致），冗余 | 已归档至 `archive/blog-platform-requirements/` |
| `docs/TEST_PLAN-v4-archived.md` | 名字带 archived 却位于 docs/ 根目录 | 已移入 `archive/` |
| `docs/06-e2e-test-report.md` | v2 时代早期报告，已被 TEST_REPORT 系列取代 | 已移入 `archive/`，README/docs/11 索引路径已同步更新 |

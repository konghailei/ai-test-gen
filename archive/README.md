# archive/ — 历史归档目录

> 存放已完成、被取代或冗余的文件。**归档 ≠ 删除**：保留供追溯，但不再是现行有效文档。
> 现行有效文档在 `docs/`（待办唯一入口：`docs/todos/CURRENT-TODOS.md`）。

## 目录结构

| 子目录/文件 | 内容 | 归档原因 |
|---|---|---|
| `ai-test-redesign/` | 管理端原型重构设计（HTML 页面集 + 设计稿） | 设计快照，已被现行前端取代 |
| `ai-testgen-update-v2.7.0/` | v2.7.0 内网更新包完整代码副本 | 版本快照，git 历史已覆盖 |
| `_design_mockups/` | 设计 mockup | 设计过程资产 |
| `blog-platform-requirements/` | 博客创作平台 PRD + v1.1 优化需求 + v1.1 澄清产物 | 与 `backend/tests/` 下活引用副本完全重复（md5 一致），冗余副本归档；现行活引用以 `backend/tests/` 为准 |
| `legacy-tests-v2/` | v2 时代 E2E 脚本与运行日志（check_*.py / e2e_test_v2*.py / *.log） | 与 `06-e2e-test-report.md` 同批的 v2 时代遗留，已被 TEST_REPORT 系列取代 |
| `06-e2e-test-report.md` | v2 时代端到端测试报告（49 项） | 已被 `TEST_REPORT*.md` + `docs/TEST_PLAN.md` 取代 |
| `TEST_PLAN-v4-archived.md` | 测试计划 v4 归档版 | 名字即归档，此前误置于 docs/ 根目录（2026-10-15 归位） |

## 归档纪律

1. 归档用 `git mv`（保留 rename 历史），并在 `docs/todos/CURRENT-TODOS.md` §四勘误台账登记；
2. 归档后同步更新所有引用方路径（README 索引、文档链引用）；
3. **文档状态滞后于代码是高发误导源**（2026-10-15 教训：docs/32 头部"定稿待实施"实际 v3.5.10 已实施）——引用任何文档状态前必须 git log + 代码双重核查。

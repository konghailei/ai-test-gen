# requirements.py 后台任务提取方案（v3.2.3 后续，待执行）

> 目标：把编排逻辑（4 个后台任务）从 api 层抽到 core 层，修复「api 反向依赖 agents / 编排逻辑错位」审计问题（#6）。路由处理器保持不动——它们本来就该在 api 层。

## 一、当前结构（已量化）

`app/api/requirements.py` 共 **71 个顶层函数**，其中 4 个后台任务合计 ~1366 行，是编排逻辑错位的核心：

| 函数 | 行号 | 行数 |
|---|---|---|
| `_run_requirement_analysis` | L1342 | 373 |
| `_run_clarify_reparse` | L2598 | 358 |
| `_run_regenerate_reparse` | L3433 | 414 |
| `_run_fp_merge_task` | L3847 | 221 |

## 二、依赖闭包（已用脚本精确分析）

4 个后台任务共调用 **8 个模块级 helper**：

| helper | 行号 | 是否也被路由处理器调用 | 处理 |
|---|---|---|---|
| `_update_task` | L1278 | 否 | 随任务搬走 |
| `_merge_interaction_srr` | L200 | 否 | 随任务搬走 |
| `_update_cross_fp_interactions_on_merge` | L479 | 否 | 随任务搬走 |
| `_save_fps_to_db` | L309 | 是（finalize_requirement） | 搬到 core，requirements.py 反向 import |
| `_save_fp_dependencies` | L664 | 是（finalize/update/confirm） | 同上 |
| `_update_fp_index_in_srr` | L603 | 是（finalize/fp_delete） | 同上 |
| `_count_clarify_rounds_since_last_regen` | L160 | 是（submit_clarify_answers） | 同上 |
| `_extract_docx_text` | L845 | 是（_parse_uploaded_files） | 同上 |

## 三、执行步骤

1. 新建 `app/core/requirement_tasks.py`，搬入：
   - 4 个 `_run_*` 后台任务
   - 8 个 helper（按上表）
   - 头部复制所需导入（`req_agent`/`clarify_agent`/`impact_agent`、`async_session`、各 `app.db.models`、`gen_task_id`/`gen_requirement_id`、`settings`、`log_operation`、`logger` 等——**逐函数核对引用，勿漏**）
2. `requirements.py` 顶部改为：
   ```python
   from app.core.requirement_tasks import (
       _run_requirement_analysis, _run_clarify_reparse,
       _run_regenerate_reparse, _run_fp_merge_task,
       _save_fps_to_db, _save_fp_dependencies, _update_fp_index_in_srr,
       _count_clarify_rounds_since_last_regen, _extract_docx_text,
   )
   ```
   删除 requirements.py 中已搬走的 12 个函数定义。
3. **注意**：8 个 helper 可能还调用其他未列出的模块级函数（本分析只展开了一层），搬移前对每个 helper 再做一次「引用的模块级名字」核对，确认无遗漏再删原定义。
4. 验证：`python -m py_compile` 全量 + phase5（101 项 API 测试覆盖上传/澄清/FP/归档路由）+ 一次真实上传→澄清→确认冒烟。

## 四、风险点

- `_extract_docx_text` 被 `_parse_uploaded_files`（文件上传路径）调用——搬走后 `requirements.py` 必须 import 回来，否则上传功能直接崩。
- 5 个「共享 helper」的循环依赖：`requirement_tasks.py` 不能 import `requirements.py`（会循环）。若某个 helper 内部又调用了 requirements.py 的路由处理器，需改为传入回调或进一步下沉。
- 搬移后 requirements.py 预计 ~4200 行，仍是最大 api 文件，但「编排逻辑错位」问题已消除；若想进一步瘦身，可继续按 `req_upload.py`/`req_tasks.py`/`req_clarify.py`/`req_fp.py`/`req_lifecycle.py` 拆路由（更机械、逐端点核验，另行排期）。

## 五、剩余未做项总览

1. ✅ clarify_agent.py 上帝文件拆分（v3.2.3 已完成）
2. ⏳ requirements.py 后台任务提取（本方案，约 2166 行）
3. ⏳ requirements.py 路由按域拆分（可选，收益低风险高，暂缓）
4. ⏳ 248+ 裸 select() 收敛 repositories（纯机械，最耗时）

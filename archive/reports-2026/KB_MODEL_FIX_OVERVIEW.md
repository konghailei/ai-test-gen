# 知识库模型就绪修复 + 历史向量化欠账补缴（2026-07-12 续）

## 背景
上轮已完成代码侧修复（`kb_task_queued` 状态、模型就绪检测 `model_status()`、单条/批量「重新向量化」端点与前端按钮）。但系统仍卡在「嵌入模型未就绪」，本轮回合实际执行运维修复并验证端到端链路。

## 根因（两层）
1. **bind mount 时序**：宿主 `./models/` 权重在容器首次启动后才下载完，Docker Desktop 未重新「看见」新目录 → 容器内 `/models/.../config.json` 缺失。
2. **torch 版本限制（第二层）**：重建挂载后模型文件可见，但本机 `torch 2.4.0+cu121` 加载 `pytorch_model.bin` 被 HuggingFace 新版安全策略拦截（要求 torch ≥ 2.6；safetensors 格式不受限）。`bge-large-zh-v1.5` 只有 `.bin`，故嵌入模型仍卡死；`bge-reranker-base` 自带 `model.safetensors`，重排不受影响。

## 执行动作
1. `docker compose up -d --force-recreate backend celery_worker` —— 重建 bind mount（仅无状态服务，数据零风险）。
2. 容器内把 `bge-large-zh-v1.5/pytorch_model.bin` 转为 `model.safetensors`（`torch.load(weights_only=False)` + `safetensors.torch.save_file`），因挂载只读，先写 `/tmp` 再 `docker cp` 到宿主。
3. 复用 `revectorize-missing` 逻辑，对 9 条「已归档但未向量化」需求派发 Celery embedding 任务。
4. 修复端点 bug：`get_all_requirements(None, 100000)` 超 Milvus `(offset+limit)≤16384` 上限 → 改为 `16384`，避免 `vectorized_ids` 恒空、统计失真。

## 验证结果
- 容器内 `model_status()` → `{"ready": true, "error": ""}`（嵌入模型成功加载）。
- 补缴核验：`total_archived=9, still_missing=0`，全部落库（Milvus 向量数由 0 增至 34，含原有条目）。
- `backend/app/api/knowledge.py` 修改后 `py_compile` 通过。

## 备注
- `reranker.py` 经核查已正确使用 `CrossEncoder`（非 FlagModel），此前标记的遗留问题已不存在。
- 后续若升级 torch 到 ≥2.6，可移除 `bge-large-zh-v1.5/model.safetensors` 兜底，但当前方案可用。
- 临时脚本 `backend/_backfill_missing.py`、`backend/_verify_missing.py` 已删除。

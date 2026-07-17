# 需求分析与用例生成检测 + 优化实施计划

> 任务：① 拉取最新代码 ② 检测需求分析功能（多次上传归档 + KB 联动 + 影响分析）③ 检测用例生成逻辑 ④ 实施全部 5 个优化
> 模式：先拉代码 → 编译验证 → 端到端检测 → 实施优化 → 验证

---

## 一、Phase 1 探索结论

### 1.1 需求分析功能检测结论（无 Bug）

| 检测项 | 状态 | 依据 |
|--------|------|------|
| 归档幂等性 | ✅ 正常 | [requirements.py](file:///d:/git_down/ai-test-gen/backend/app/api/requirements.py#L2041-L2045) L2041：archived 状态拒绝重复归档（409） |
| 主需求向量写入 | ✅ 正常 | L2100-2126：Celery 异步写入 Milvus，失败时同步降级 |
| FP 向量写入 | ✅ 正常 | L2165-2204：归档时将 FP 以 `FP-{fp_id}` 写入 Milvus |
| 向量化状态回写 | ✅ 正常 | L2148-2161：回写 vectorization_status 到 PG |
| KB 联动（新需求检索旧归档） | ✅ 正常 | ClarifyAgent._search_similar_requirements 检索 KB |
| Milvus upsert 幂等 | ✅ 正常 | [milvus_store.py](file:///d:/git_down/ai-test-gen/backend/app/db/milvus_store.py#L312-L326) L312-326：先删后插，避免重复 |
| 影响分析触发 | ✅ 正常 | [orchestrator.py](file:///d:/git_down/ai-test-gen/backend/app/core/orchestrator.py#L519-L555) L519-555：用例生成流程中 impact 节点触发 |
| 影响分析条件 | ✅ 正常 | 仅优化需求（含 optimization_context）时执行 |

**结论**：需求分析全流程（多次上传 → 归档 → KB 联动 → 影响分析）功能正常，无 Bug。

### 1.2 用例生成逻辑检测结论（5 个优化点）

| 优化点 | 现状 | 影响 |
|--------|------|------|
| ① 用例写入 Milvus 串行 | [gen_agent.py](file:///d:/git_down/ai-test-gen/backend/app/agents/gen_agent.py#L142-L161) L142-161：每用例单独 `await asyncio.to_thread` | N 个用例需 N 次串行 IO，耗时长 |
| ② 无旧用例清理 | 重新生成创建新 ID，不清理旧用例 | 同一 req_id 产生大量重复用例 |
| ③ max_tokens 固定 8192 | [gen_agent.py](file:///d:/git_down/ai-test-gen/backend/app/agents/gen_agent.py#L114) L114 | 大需求用例数不足 |
| ④ 无 LLM 缓存 | [llm_client.py](file:///d:/git_down/ai-test-gen/backend/app/core/llm_client.py#L217) L217 | 相同 prompt 重复调用浪费成本 |
| ⑤ Few-shot 阈值硬编码 0.7 | [gen_agent.py](file:///d:/git_down/ai-test-gen/backend/app/agents/gen_agent.py#L184) L184 | 无法调优，可能过滤有用用例 |

---

## 二、执行计划

### Step 1：拉取最新代码
```bash
cd d:\git_down\ai-test-gen
git pull
```

### Step 2：编译验证
- 后端：`docker exec ai-test-gen-api python -c "from app.api.requirements import router; print('OK')"`
- 前端：`cd frontend; npx tsc --noEmit`

### Step 3：实施 5 个优化

#### 优化 1：用例写入 Milvus 并发化
**文件**：`backend/app/agents/gen_agent.py` L138-161
**改动**：将串行 `await asyncio.to_thread` 改为 `asyncio.gather` 并发写入
**原理**：N 个用例并发写入，耗时从 N×T 降为 max(T)

```python
# 修改前（串行）
for case in cases:
    try:
        await asyncio.to_thread(vector_store.add_test_case, ...)
        vb_success += 1
    except Exception as e:
        vb_fail += 1

# 修改后（并发）
async def _write_one(case):
    try:
        await asyncio.to_thread(vector_store.add_test_case, case_id=case["id"], ...)
        return True
    except Exception:
        return False

results = await asyncio.gather(*[_write_one(c) for c in cases])
vb_success = sum(1 for r in results if r)
vb_fail = sum(1 for r in results if not r)
```

#### 优化 2：重新生成时按 req_id 清理旧用例
**文件 1**：`backend/app/db/milvus_store.py`（新增方法）
**改动**：新增 `delete_cases_by_req_id(req_id)` 方法
```python
def delete_cases_by_req_id(self, req_id: str) -> int:
    """按 req_id 批量删除用例向量，返回删除数量。"""
    self._ensure_connected()
    collection = self._get_collection("test_cases")
    collection.delete(f'metadata["req_id"] == "{_safe_id(req_id, "req_id")}"')
    collection.flush()
    return 0  # Milvus delete 不返回计数
```

**文件 2**：`backend/app/agents/gen_agent.py` L86（generate 方法开头）
**改动**：在生成新用例前清理旧用例
```python
# 在 KB 检索前，清理该 req_id 的旧用例向量（避免重复）
try:
    await asyncio.to_thread(vector_store.delete_cases_by_req_id, req_id)
    logger.info("[GenAgent] 已清理旧用例向量: req_id={}", req_id)
except Exception as e:
    logger.debug("[GenAgent] 清理旧用例失败（不阻断）: {}", str(e)[:120])
```

**文件 3**：`backend/app/db/models.py`（可选，PG 层清理）
**改动**：在 generate 端点或 GenAgent 中清理 PG 中的旧 TestCase 记录

#### 优化 3：分批生成（max_tokens 动态调整）
**文件**：`backend/app/agents/gen_agent.py` L109-115
**改动**：根据 FP 数量动态调整 max_tokens
```python
# 根据 FP 数量动态调整 max_tokens
fp_count = len(functional_points or [])
if fp_count > 10:
    max_tokens = 16384  # 大需求翻倍
elif fp_count > 5:
    max_tokens = 12288
else:
    max_tokens = 8192  # 默认
```

#### 优化 4：LLM 缓存机制
**文件**：`backend/app/core/llm_client.py` L217（call 方法）
**改动**：基于 prompt hash 的内存缓存（LRU）
```python
# 在 call 方法开头添加缓存检查
import hashlib
cache_key = hashlib.md5(f"{prompt}|{system_prompt}|{temperature}|{model}".encode()).hexdigest()
if cache_key in self._response_cache:
    logger.debug("[LLM] 缓存命中: key={}", cache_key[:12])
    return self._response_cache[cache_key]

# 调用成功后写入缓存
result = await self._call_with_retry(...)
if len(self._response_cache) > 100:  # LRU 限制 100 条
    self._response_cache.pop(next(iter(self._response_cache)))
self._response_cache[cache_key] = result
return result
```
**初始化**：`__init__` 中添加 `self._response_cache: dict[str, str] = {}`

#### 优化 5：Few-shot 阈值可配置
**文件 1**：`backend/app/config.py`（新增配置项）
```python
FEW_SHOT_SIMILARITY_THRESHOLD: float = 0.70  # GenAgent Few-shot 相似度阈值
```
**文件 2**：`backend/app/agents/gen_agent.py` L184
```python
# 修改前
return [r for r in results if r.get("similarity", 0) > 0.7]
# 修改后
from app.config import settings
return [r for r in results if r.get("similarity", 0) > settings.FEW_SHOT_SIMILARITY_THRESHOLD]
```

### Step 4：编译验证
- 后端编译：`docker exec ai-test-gen-api python -c "from app.agents.gen_agent import GenAgent; print('OK')"`
- 前端类型检查：`cd frontend; npx tsc --noEmit`

### Step 5：重启服务
```bash
docker restart ai-test-gen-api ai-test-gen-celery
```

### Step 6：端到端验证
1. 健康检查
2. 登录
3. 上传需求（raw_text 异步模式）
4. 等待分析完成
5. 生成用例（验证 5 个优化生效）
6. 验证旧用例清理（重新生成后用例数不重复）
7. 验证 KB 联动（归档后新需求能检索到）

---

## 三、Assumptions & Decisions

### Assumptions
1. git pull 不会有冲突（上次已是最新）
2. Docker 服务正常运行
3. LLM API Key 有效

### Decisions
1. **优化 2 清理策略**：在 GenAgent.generate 开头清理，确保每次生成都是"干净"的。不改变"生成用例"的结果，只避免重复堆积。
2. **优化 4 缓存策略**：使用进程内 LRU 字典（100 条），不引入 Redis 依赖。仅缓存相同 prompt 的响应，temperature=0 的调用受益最大。
3. **优化 3 max_tokens**：上限 16384，避免过度消耗。分批生成暂不实施（改动太大），仅动态调整 token 上限。
4. **不改变功能逻辑**：所有优化仅提升性能/可维护性，不改变用例生成的业务结果。

---

## 四、Verification Steps

1. **编译验证**：后端导入无错误 + 前端 tsc 无错误
2. **功能验证**：上传需求 → 生成用例 → 用例数 > 0
3. **优化 1 验证**：日志中用例写入耗时显著降低
4. **优化 2 验证**：重新生成后，Milvus 中该 req_id 的用例数不翻倍
5. **优化 3 验证**：大需求（FP>10）日志显示 max_tokens=16384
6. **优化 4 验证**：日志出现"缓存命中"
7. **优化 5 验证**：修改 config.py 阈值后，Few-shot 数量变化

---

## 五、风险与回滚

- 所有优化均为性能提升，不改变业务逻辑
- 若优化引入问题，可逐个回滚（每个优化独立）
- 优化 2（清理旧用例）需谨慎：确保只在重新生成时清理，不影响已归档用例的 KB 检索

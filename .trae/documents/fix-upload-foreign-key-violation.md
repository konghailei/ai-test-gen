# 修复需求上传 ForeignKeyViolationError - 实施计划

## 问题
上传需求时报错：
```
asyncpg.exceptions.ForeignKeyViolationError: insert or update on table "analysis_tasks" violates foreign key constraint "analysis_tasks_req_id_fkey"
DETAIL:  Key (req_id)=(REQ-1784007821774-9402) is not present in table "requirements".
```

## 根因
在 `backend/app/api/requirements.py` 的 `upload_requirement_multi` 中：
1. 先 `session.add(record)` 创建 Requirement
2. 再 `session.add(task)` 创建 AnalysisTask（req_id 指向上面的 Requirement）
3. 最后 `await session.commit()`

虽然看似顺序正确，但 SQLAlchemy 的 flush/commit 顺序没有显式保证先插入 Requirement。PostgreSQL 外键约束要求插入 AnalysisTask 时目标 Requirement 行已存在，导致 violation。

## 修复方案
在创建 AnalysisTask 之前，显式调用 `await session.flush()` 让 Requirement 先写入数据库。

```python
session.add(record)
await session.flush()  # 确保 Requirement 先落库
session.add(task)
await session.commit()
```

## 同样问题检查
搜索代码中所有"先 add 父表记录、再 add 带外键的子表记录、然后 commit"的模式，确认是否还有其他地方会触发同样问题。

## 验证
1. 重新上传需求，确认不再报 ForeignKeyViolationError
2. 确认 AnalysisTask 记录正常创建
3. 确认任务状态查询返回正确

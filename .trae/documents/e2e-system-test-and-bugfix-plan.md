# 端到端系统测试与问题修复实施计划

> Status: DRAFT v2（更新版）
> Source: 用户请求 `/plan` + 历史对话修复经验
> Created: 2026-08-07
> Target: 新建隔离产品 → 构造 4 份需求文档 → 验证 7 大功能模块 → 问题零侵入修复

---

## 一、用户需求总结（Requirements Summary）

用户需要对 **ai-test-gen 系统**执行一次完整的端到端功能测试，覆盖从产品创建到影响分析的全链路：

1. **新建隔离产品**：创建独立产品/项目/模块，避免污染现有数据
2. **构造并上传 4 份需求文档**：设计版本演进关系（v1 新建 → v2 优化 → v3 小修订）和场景（矛盾检测、引用未定义、优化类触发）
3. **验证归档正确性**：项目/需求/FP 三级状态流转、Milvus 向量入库、PG status、hybrid_search 按 archived 过滤
4. **验证需求分析完善性**：SRR 结构符合 hard constraints、_raw_text 完整、FP 拆分通过 FPGate、不破坏整体分析框架
5. **验证澄清问题精准性**：猜想类/过宽类被过滤、优化影响仅 2 条排末尾、引用未定义文本精简、KB 和人工共用一套问题逻辑
6. **验证 KB 存在时自动回答 + SRR 合并正确**：v1 归档后 v2 引用未定义被 KB 命中、merged SRR 仅追加新增字段、不覆盖原有值、cross_fp_flows 等字段 unique append
7. **验证多次上传影响范围分析正确**：v2/v3 上传后影响分析精准、不过度扩散、回归用例正确分类为「回归测试」
8. **问题零侵入修复**：任何 bug 修复不破坏需求分析整体逻辑，修复后双验证（旧单测 + 新回归）全通过

---

## 二、验收标准（Acceptance Criteria, AC-1 ~ AC-8）

### AC-1：产品/项目/模块创建成功且互相隔离
- [ ] 新建产品 `E2E-测试产品 20260807` 返回 product_id，PG `products` 表有记录
- [ ] 创建项目 A1（走完整归档路径）和 A2（走 `active → pending_archive → cancel → active → archive` 状态循环）
- [ ] 创建模块 M1（用户管理）、M2（内容管理）、M3（版本管理）
- [ ] A1/A2 需求互相隔离：hybrid_search 按 product_id 查询不泄漏
- [ ] A1 归档后，其下 100% 需求和 FP 状态同步为 `archived`

### AC-2：4 份测试需求文档设计合理，覆盖全部场景
| 文档 | 场景设计 | 预期触发检测 |
|---|---|---|
| ReqA_v1_PRD.md | 新建平台 PRD：明确给出「图片上传规则：格式 JPG/PNG/GIF + 单文件 ≤ 10MB + 超限提示『图片过大』」；「手动标记版本最多保留 50 个」；用户/内容/版本三模块；happy_path 4 步；8 条验收标准 | SRR 结构化、_raw_text 完整解析 |
| ReqA_v2_Optimize.md | 优化需求：正文写「沿用现有图片上传规则」+「不设版本数量上限」；多处包含『升级/优化/改造/兼容已有数据』关键词 | ① 矛盾检测（50 vs 不设上限）② 引用未定义（图片上传规则）③ 优化影响配套问题 2 条排末尾 |
| ReqA_v3_SmallFix.md | 小修订：「正文改动 < 5% 的小修订免审核」（5% 标准未定义）；引用 v1 的敏感词策略但不展开；happy_path 存在但字段略少 | ① 5% 判定标准追问 ② 过宽实体字段定义被过滤 ③ 猜想类 0 条 |
| ReqB_PRD.md | 同产品下完全无关：简单审批流 PRD | 跨需求污染：hybrid_search 按 product_id 过滤只命中对应文档 |

### AC-3：归档正确性（7 项子指标）
1. [ ] `archive_project` 后 `projects.status = archived`
2. [ ] `projects/{id}/kb/entries` 返回数量 = 需求数 + FP 数，全部 `status = archived`
3. [ ] Milvus `product_id = A1` 查询向量数 > 0，其它 product_id = 0
4. [ ] `hybrid_searcher.search_requirements(query="图片上传规则 格式 大小 限制", product_id=A)` top3 至少 1 条命中 ReqA_v1
5. [ ] `cancel-pending-archive` 正确回滚不残留 archived 标记
6. [ ] `reactivate` 恢复后 hybrid_search 不再把该需求作为 KB 历史返回
7. [ ] `archive_requirement` 单需求归档正确，同级需求不受影响

### AC-4：需求分析完善且不破坏整体逻辑
- [ ] SRR JSON 结构：`product_id` 正确 + `modules[]` 复数（不是单数 module）
- [ ] `flows.happy_path.steps` 数量 = 原始文档步骤数（不能缺、不能凭空加倍）
- [ ] `acceptance_criteria` 结构化 + `_raw_text` 全文 Markdown 列表完整
- [ ] FP 拆分通过 FPGate 三项检查（fp 非空、字段完整、唯一性）
- [ ] 状态流转：`analyzing → draft(needs_clarification) → pending_review → confirmed → archived`，无新增状态

### AC-5：澄清问题精准、无多余
**ReqA_v2_Optimize 断言**：
1. [ ] 矛盾问题存在且仅 1 条：category=`conflict`，question 含「50」和「上限」或「不设」
2. [ ] 引用未定义问题存在且仅 1 条：`len(question) ≤ 120`，不含「raw_text#」来源噪音
3. [ ] 优化影响配套问题恰好 2 条：id ∈ {`kb_optimization_impact`, `kb_optimization_impact_scope`}，且在 questions 列表**最后 2 个位置**
4. [ ] 猜想类问题 0 条：扫描全部 question 不匹配 `超过.*怎么办|若.*大于|大于.*上限|假设.*超限`

**ReqA_v3_SmallFix 断言**：
5. [ ] 「5%改动比例判定标准」ambiguity 问题 ≥ 1 条
6. [ ] 过宽追问（实体字段定义/happy_path 穷举/性能穷举）0 条

### AC-6：KB 存在时自动回答 + SRR 合并正确
- 前置条件：ReqA_v1 已归档（Milvus + PG 双写）
- 执行：上传 ReqA_v2 进入 clarify 阶段
- 断言：
  1. [ ] 「图片上传规则引用未定义」问题的 `resolved=true 且 answered=true`（由 KB 路径回答，非人工）
  2. [ ] v2 augmented_srr 中图片上传规则字段 = v1 原文字段值（格式 3 项 + 大小 + 超限文案完全一致）
  3. [ ] v2 augmented_srr 中原有字段（`requirement.title / id / flows.happy_path.steps` 等）与 KB 回答前**完全相同**（未被覆盖）
  4. [ ] `cross_fp_flows / shared_business_rules` 等跨 FP 字段：值只能 unique append，绝不能 overwrite
  5. [ ] `gen_confidence` 字段位置 = `gen_metadata.gen_confidence`（不在顶层）

### AC-7：多次上传影响范围分析正确
- [ ] ReqA_v2 归档后触发 impact-analysis：受影响 FP 数 > 0 且 < 「v1 FP 总数 × 60%」（防止过度扩散）
- [ ] 至少 1 个 FP 影响原因 = 「改动触及图片上传规则 / 版本数量上限调整」
- [ ] 确认影响后 `/generate` 生成用例：受影响 FP 的用例 `type=regression`，分类目录 = 「回归测试」
- [ ] ReqA_v3 上传后 impact-analysis：仅列出与小修订直接关联的 FP，不产生全局影响

### AC-8：问题修复的零侵入性
- [ ] 修复不修改路由文件（`backend/app/api/*.py` 的 URL 映射不动）
- [ ] 修复不改 SRR 顶层 schema，辅助字段必须 `_xxx` 下划线前缀且持久化前清理
- [ ] 修复后 `pytest backend/tests -x -q` 全通过
- [ ] 每条 bug 新增 1 条最小 pytest 回归用例到 `backend/tests/test_e2e_regression.py`
- [ ] `git diff --stat` 被修改文件 ≤ 5 个（白名单：clarify_agent.py / hybrid_search.py / tests/* / utils/*）

---

## 三、实施步骤（Implementation Steps）

### Phase 0：环境预检 & 数据准备（不改代码，纯检查）

#### Step 0-1：容器健康检查
```
cd d:\git_down\ai-test-gen
docker-compose ps 2>&1 | tee _e2e_tmp/docker_ps_before.log
```
- api / celery / postgres / milvus 四个容器必须 `Up (healthy)`
- 若异常：`docker-compose restart api celery` 重试 3 次，仍失败 → 降级 Option B（后端集成测试）

#### Step 0-2：工作区状态快照
```
git status --short > _e2e_tmp/git_before.log
git branch --show-current >> _e2e_tmp/git_before.log
```
- 若 working tree dirty：先询问用户是否 stash/commit 已有改动，防止测试代码混进去

#### Step 0-3：后端导入检查
```
cd backend
python -c "from app.agents.clarify_agent import ClarifyAgent; print('agent import OK')"
python -c "from app.db.hybrid_search import hybrid_searcher; print('hybrid OK')"
python -c "from app.core.fp_gate import FPGate; print('fp-gate OK')"
```
- 三项全部 OK 才进入 Option A，否则降级 Option B

---

### Phase 1：构造 4 份测试需求文档（纯数据）

**输出目录**：`backend/_e2e_tmp/docs/`（加入 .gitignore）

每份文档长度 500~1200 字，Markdown 列表验收标准 ≥ 6 条，`_raw_text` 解析 ≥ 3 个章节。

#### Doc 1：`A_E2ETEST_ReqA_v1_PRD.md`
核心设计（确保规则层矛盾检测和 KB 查询能命中）：
```markdown
# 内容创作平台 PRD（v1.0）

- 产品：内容创作平台
- 模块：用户管理、内容管理、版本管理

## 功能需求
### 图片素材上传
- 支持 JPG / PNG / GIF 三种格式
- 单文件大小不超过 10MB
- 超限时提示：「图片过大，请压缩后重新上传」

### 手动版本管理
- 用户可手动标记历史版本，最多保留 50 个标记版本

## 正常流程（Happy Path）
1. 用户登录系统进入工作台
2. 点击「上传素材」选择图片
3. 系统校验格式和大小后上传成功
4. 用户标记为正式版本后保存

## 业务规则
- BR-01：上传必须校验格式和大小
- BR-02：保留版本数超 50 时，自动淘汰最早标记版本

## 验收标准
- AC-01：JPG/PNG/GIF 均能上传成功
- AC-02：单文件 > 10MB 时提示「图片过大」
- AC-03：手动标记版本数 ≤ 50 全部保留
- AC-04：超过 50 时最早版本被自动淘汰
- AC-05：happy path 四步全通
- AC-06：敏感词校验后内容才入库
```

#### Doc 2：`A_E2ETEST_ReqA_v2_Optimize.md`
核心设计（触发矛盾 + 引用未定义 + 优化类）：
```markdown
# 内容创作平台 功能优化需求（v2.0）

- 产品：内容创作平台
- 模块：内容管理、版本管理
- 类型：优化改造

## 改造背景
基于 v1.0 平台进行功能升级，兼容现有历史数据。

## 功能变更
### 素材上传优化
- 沿用现有图片上传规则，本次不改动
- 新增批量上传（一次最多 20 张）

### 版本数量策略调整
- 为提升用户体验，手动标记版本**不设数量上限**，不再自动淘汰旧版本

## 业务规则升级
- 兼容 v1.0 所有已归档历史版本
- 历史数据迁移后保持原有标记状态

## 验收标准
- AC-01：图片上传规则与现有完全一致
- AC-02：批量 20 张上传成功
- AC-03：版本数量无上限，旧版本不再自动淘汰
```

#### Doc 3：`A_E2ETEST_ReqA_v3_SmallFix.md`
核心设计（触发 5% 未定义 + 小改动不过度影响）：
```markdown
# 内容创作平台 小修订需求（v2.1）

- 产品：内容创作平台
- 模块：内容管理
- 类型：小修订

## 修订内容
正文改动 < 5% 的小修订免审核，直接发布。

沿用 v1 敏感词校验策略，此处不重复。

## 正常流程
1. 用户编辑已发布内容
2. 系统计算改动比例
3. < 5% 直接发布，否则进入审核

## 验收标准
- AC-01：小修订免审核直接发布
- AC-02：敏感词策略正常生效
```

#### Doc 4：`A_E2ETEST_ReqB_PRD.md`（污染对照组）
```markdown
# 简单审批流 PRD

- 产品：内容创作平台
- 模块：审批管理

## 功能
- 提交审批 → 一级审批 → 二级审批 → 通过/驳回

## 验收标准
- AC-01：一级审批人可通过
- AC-02：二级审批人可驳回
```

---

### Phase 2：登录 → 建产品/项目/模块（API 调用）

#### Step 2-1：登录获取 Token
```python
# httpx 调用 POST /api/auth/login
# 默认 admin/admin（若不存在则从 DB 查用户表）
POST /api/auth/login body={"username":"admin","password":"admin"}
→ 保存 Authorization: Bearer {token}
```
- 失败回退：直接从 DB users 表取 admin id + 生成临时 JWT（仅测试产品内使用）

#### Step 2-2：创建产品/项目/模块
```
POST /api/products body={name:"E2E-测试产品 20260807", description:"隔离测试产品，可安全删除"}
→ 保存 product_id (pid)

POST /api/products/{pid}/projects body={name:"A1-完整归档路径", version:"1.0"}
POST /api/products/{pid}/projects body={name:"A2-归档状态循环", version:"1.0"}
→ 保存 proj1_id, proj2_id

POST /api/products/{pid}/modules body={name:"用户管理", description:"用户与权限"}
POST /api/products/{pid}/modules body={name:"内容管理", description:"内容上传与审核"}
POST /api/products/{pid}/modules body={name:"版本管理", description:"历史版本与标记"}
→ 保存 m1_id, m2_id, m3_id
```
- 全部 ID 写入 `_e2e_tmp/ids.json`，后续步骤引用

---

### Phase 3：ReqA_v1 上传 → 分析 → 补全澄清 → 归档

#### Step 3-1：上传 ReqA_v1
```
POST /api/requirements/upload
  FormData:
    file: A_E2ETEST_ReqA_v1_PRD.md
    project_id: proj1_id
    modules: [m1_id, m2_id, m3_id]
→ 返回 req1_id + task1_id
```

#### Step 3-2：轮询任务直到完成
```
GET /api/tasks/{task1_id}
轮询间隔 3s，超时 240s
status ∈ {success, failed, pending}
```
- 若 failed：立即从 `_error_log` 提取错误信息 + 保存 clarify agent trace → 诊断是否 bug

#### Step 3-3：SRR 完整性检查
```
GET /api/requirements/{req1_id} → 保存 reqA_v1_srr.json
```
Python 断言：
```python
srr = resp.json()["data"]["srr"]
assert srr["requirement"]["product_id"] == pid
assert set(srr["requirement"]["modules"]) == {m1_id, m2_id, m3_id}
assert len(srr["_raw_text"]) > 500
assert len(srr["flows"]["happy_path"]["steps"]) >= 3
assert "图片" in srr["_raw_text"] and "10MB" in srr["_raw_text"]
```

#### Step 3-4：补全澄清（若有）
```
GET /api/requirements/{req1_id}/clarify/questions
→ 若 questions 非空，POST /api/requirements/{req1_id}/clarify body={answers: [...]}
→ 等待 _run_clarify_reparse 完成（轮询 status 从 needs_clarification → draft）
```

#### Step 3-5：状态流转到归档
```
POST /api/requirements/{req1_id}/finalize     → status: pending_review
POST /api/requirements/{req1_id}/confirm      → status: confirmed
POST /api/requirements/{req1_id}/archive      → status: archived（go_live_date = 今天 - 3 天）
```
**关键**：go_live_date 设置为今天 - 3 天，v2/v3 设置为今天 / 今天 + 1h → 时间衰减排序验证（最新版本排前）

#### Step 3-6：AC-3 归档正确性断言（7 子项）
```python
# 1) PG status=archived
SELECT status, count(*) FROM requirements WHERE project_id=proj1_id GROUP BY status;
# 预期：仅 archived 一行 count = 1

# 2) Milvus 向量存在
from app.db.milvus_store import milvus_store
count = milvus_store.count(product_id=pid)
assert count > 0

# 3) hybrid_search 短 query 命中
res = hybrid_searcher.search_requirements(
    query="图片上传规则 格式 大小 限制",
    product_id=pid,
    project_id=proj1_id,
)
assert any(req1_id in r.id for r in res[:3])

# 4) Milvus 产品隔离
count_other = milvus_store.count(product_id="other-product-fake-id")
assert count_other == 0
```

---

### Phase 4：ReqA_v2 上传 → 重点验证澄清质量 + KB 自动回答（AC-5 + AC-6）

#### Step 4-1：上传 ReqA_v2（流程同 Step 3-1/2）
→ 返回 req2_id + task2_id

#### Step 4-2：保存澄清问题 + KB 日志
```
GET /api/requirements/{req2_id}/clarify/questions → 保存 reqA_v2_questions.json
GET /api/requirements/{req2_id} → srr._kb_access_log 保存 reqA_v2_kb_log.json
```

#### Step 4-3：AC-5 断言（澄清质量 6 项）
```python
qs = reqA_v2_questions["data"]["questions"]

# (1) 矛盾问题且仅 1 条
conflicts = [q for q in qs if q.get("category")=="conflict" or "矛盾" in q.get("question","")]
assert len(conflicts) >= 1
assert any("50" in q["question"] and ("上限" in q["question"] or "不设" in q["question"]) for q in conflicts)

# (2) 引用未定义 1 条且精简
refundef = [q for q in qs if "引用未定义" in q.get("question","") or "图片上传规则" in q.get("question","")]
assert len(refundef) >= 1
assert len(refundef[0]["question"]) <= 120
assert "raw_text#" not in refundef[0]["question"]

# (3) 优化影响恰好 2 条排末尾
opt_ids = {"kb_optimization_impact", "kb_optimization_impact_scope"}
opt_qs = [q for q in qs if q.get("id") in opt_ids]
assert len(opt_qs) == 2
assert {q["id"] for q in qs[-2:]} == opt_ids  # 最后 2 个

# (4) 猜想类 0 条
import re
spec_re = re.compile(r"超过.*怎么办|若.*大于|大于.*上限|假设.*超限|如果.*突破|万一.*超过")
assert not any(spec_re.search(q.get("question","")) for q in qs)
```

#### Step 4-4：AC-6 断言（KB 回答 + SRR 合并）
```python
# (1) 引用未定义问题 answered=true
refundef_q = refundef[0]
assert refundef_q.get("resolved") == True
assert refundef_q.get("answered") == True
assert refundef_q.get("_answer_source") == "kb"  # 或等价字段

# (2) 图片上传规则字段与 v1 完全一致
GET req2_id after clarify → srr_augmented
assert "JPG/PNG/GIF" in srr_augmented 或对应字段值 == v1 原文
assert "10MB" in srr_augmented 或对应字段值 == v1 原文
assert "图片过大" in srr_augmented 或对应字段值 == v1 原文

# (3) 原字段未被覆盖（字节级对比）
before = srr_before_kb_answer  # clarify 前保存的快照
after = srr_augmented
for field in ["requirement.title", "requirement.id", "flows.happy_path.steps"]:
    assert nested_get(before, field) == nested_get(after, field)

# (4) cross_fp_flows 等字段 unique append
# 如果 v1 有 3 条 cross_fp_flows，v2 只能 ≥ 3 且重复项不重复出现
```

---

### Phase 5：补全 v2 澄清 → 归档 → 上传 v3 → 影响分析（AC-7）

#### Step 5-1：补全 v2 人工澄清 → 归档
- 矛盾项（KB 无法回答）提交合理答案（如「统一改为保留最多 200 个版本」）
- 走 clarify_reparse → finalize → confirm → archive 完整链路
- v2 go_live_date = 今天

#### Step 5-2：上传 v3 → AC-5 对 v3 断言
- 「5% 改动比例」问题 ≥ 1 条
- 过宽实体字段类 0 条
- 猜想类 0 条

#### Step 5-3：触发影响分析 → AC-7 断言
```
POST /api/requirements/{req3_id}/impact-analysis
body={auto_detect: true}  # 或不选 FP，走智能检测
→ 保存 impact_v3.json
```
```python
affected_fps = impact_v3["data"]["affected_functional_points"]
total_fp_v1 = count_from_db(req1_id)
assert 0 < len(affected_fps) < total_fp_v1 * 0.6

# 至少 1 条影响原因命中关键字
assert any(
    "图片上传" in fp.get("impact_reason","") or "版本数量" in fp.get("impact_reason","")
    for fp in affected_fps
)
```

#### Step 5-4：确认影响 → 生成回归用例
```
POST /api/requirements/{req3_id}/impact-analysis/confirm
body={confirmed_fp_ids: [...]}
→ 触发 /generate → 保存 testcases_v3.json
```
```python
regression_cases = [c for c in testcases_v3 if c.get("type") == "regression"]
assert len(regression_cases) >= len(affected_fps) * 0.8  # 受影响 FP 至少 80% 有回归用例
assert any("回归测试" in c.get("category","") for c in regression_cases)
```

---

### Phase 6：问题诊断与零侵入修复（仅在 AC 失败时）

**禁止跳步，必须按以下 6 步执行：**

1. **留证优先**：把失败 AC 的所有请求/响应/SRR/questions/SQL/Milvus 结果保存到 `_e2e_tmp/bug_{BUGNAME}/`
2. **根因定位**：对比 v2.8.67 修复点 + project_memory hard constraints，判断根因类型：
   - 规则层漏检 → `_detect_logical_contradictions` 内增强
   - LLM 语义问题 → `SYSTEM_PROMPT` 或后置过滤器
   - KB 检索失败 → `hybrid_searcher` query 生成 / product_id 过滤
   - SRR 合并错误 → `_merge_kb_answer_into_srr` 字段级 merge 逻辑
3. **最小补丁提案**：写 ≤ 5 行修复说明 → 自查 8 条 hard constraints：
   - ❌ 路由文件绝对不改
   - ❌ SRR 顶层绝对不改
   - ❌ ClarifyAgent 主流程 ①~⑨ 顺序绝对不变
4. **实施补丁**：修改对应单一函数 + 写 1 条 pytest 最小回归用例
5. **双验证**：
   ```
   pytest backend/tests -x -q  # 必须全通过
   # 然后单独重跑失败的 Step X 场景
   ```
6. **留痕归档**：`git diff > _e2e_tmp/diff_{BUGNAME}.patch`，记录修改前后对比

---

### Phase 7：Re
[truncated by convert_rollouts_to_sft: original content length=11280 chars for checker-safe SFT export]

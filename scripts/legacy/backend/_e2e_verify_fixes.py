# -*- coding: utf-8 -*-
"""
v2.55 Bug 修复 E2E 验证
1. 列出 products / projects / modules 可选
2. 提交一个极简需求让流水线跑起来
3. 完成后检查：
   - Bug A-1: functional_points.module_id / modules 字段
   - Bug C-1: requirements.search_vec 非空（触发器自动维护）
   - Bug B-1: 依赖图谱 include_inactive 默认 True
   - Bug C-2: /api/health 的 kb_unavailable / kb_empty 返回结构
4. 清理测试数据（可选）
"""
import asyncio
import json
import sys
import time
import requests

API_BASE = "http://localhost:8000/api"
S = requests.Session()


def step(title):
    sep = "=" * 60
    print(f"\n{sep}\n{title}\n{sep}")


def pp(data, n=800):
    s = json.dumps(data, ensure_ascii=False, indent=2)
    print(s[:n] + ("..." if len(s) > n else ""))


# ---------------------------------------------------------------------------
# Step 1: 查 catalog（无需登录？试试绕过 auth）
# ---------------------------------------------------------------------------
step("1. /health — 检查 C-2 kb_status 结构（kb_unavailable / kb_empty）")
r = S.get(f"{API_BASE}/health", timeout=10)
print("Status:", r.status_code)
pp(r.json())
health = r.json()
health_data = health.get("data", {})
# v2.55 C-2: 区分 kb_unavailable 与 kb_empty
if "kb_empty" in health_data:
    print(f"  ✓ kb_empty = {health_data.get('kb_empty')}")
else:
    print("  ⚠ kb_empty 字段缺失")

# ---------------------------------------------------------------------------
# Step 2: 找可用产品（如果有 products API）
# ---------------------------------------------------------------------------
step("2. 列出 products & modules（直接查 DB，用 asyncpg）")
import asyncpg

DB_URL = "postgresql://aitestgen:aitestgen123@localhost:5432/ai_test_gen"


async def list_context():
    conn = await asyncpg.connect(DB_URL)
    try:
        prods = await conn.fetch("SELECT id, name, status FROM products LIMIT 10")
        print("\n  [products]")
        for p in prods:
            print(f"    id={p['id']:22s}  name={p['name']:20s}  status={p['status']}")

        projs = await conn.fetch("SELECT id, name, product_id, status FROM projects LIMIT 10")
        print("\n  [projects]")
        for p in projs:
            print(f"    id={p['id']:22s}  name={p['name']:20s}  product_id={p['product_id']:22s}  status={p['status']}")

        mods = await conn.fetch("SELECT id, product_id, name FROM modules LIMIT 20")
        print("\n  [modules]")
        for m in mods:
            print(f"    id={m['id']:22s}  product_id={m['product_id']:22s}  name={m['name']}")
        return prods, projs, mods
    finally:
        await conn.close()


prods, projs, mods = asyncio.run(list_context())
if not prods:
    print("⚠ 没有产品，无法继续 E2E，请手动建立产品-模块后再跑验证")
    sys.exit(0)

pick_prod = prods[0]
product_id = pick_prod["id"]
pick_mod = mods[0] if mods else None
print(f"\n  选择 product = {pick_prod['name']} (id={product_id})")
if pick_mod:
    print(f"  推荐 module  = {pick_mod['name']} (id={pick_mod['id']})")

# ---------------------------------------------------------------------------
# Step 3: 提交一个极简需求，触发流水线（走 analyze_requirement API）
# ---------------------------------------------------------------------------
step("3. 提交测试需求（调用 /requirements/analyze SSE）")

TEST_REQ_TEXT = f"""
# 测试需求 - Bug修复验证（A-1/C-1/B-1 等）

## 背景
本需求为 v2.55 修复验证专用。

## 功能点
### FP1 用户登录
- 用户输入账号密码后提交表单
- 系统校验账号密码正确性，通过后跳转到首页
- 错误时弹出提示
- 建议模块：{"用户认证模块" if not pick_mod else pick_mod['name']}

### FP2 用户登出
- 登录状态下点击登出按钮清除会话
- 跳转至登录页面
- 建议模块：{"用户认证模块" if not pick_mod else pick_mod['name']}

### FP3 密码重置
- 用户通过邮箱接收验证码重置密码
- 验证码 10 分钟内有效
- 建议模块：{"用户认证模块" if not pick_mod else pick_mod['name']}
""".strip()

# 先尝试不带认证（若项目关闭了 auth），如 401 则通知用户手动 token
analyze_url = f"{API_BASE}/requirements/analyze"
payload = {
    "product_id": product_id,
    "requirement_text": TEST_REQ_TEXT,
    "priority": "P2",
    "project_id": (projs[0]["id"] if projs else None),
    "title": "v2.55修复验证-E2E测试",
}
print(f"  POST {analyze_url}")
print(f"  payload product_id={product_id}, title={payload['title']}")

req_id = None
task_id = None
try:
    with S.post(analyze_url, json=payload, timeout=10, stream=True) as resp:
        print("  HTTP", resp.status_code, resp.headers.get("content-type", ""))
        if resp.status_code != 200:
            print(resp.text[:2000])
            raise SystemExit(resp.status_code)
        # SSE 读取，取最后 requirement_id / task_id
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    obj = json.loads(chunk)
                except Exception:
                    # 多行 JSON 拼接的话忽略
                    continue
                evt = obj.get("event") or obj.get("type")
                rid = obj.get("requirement_id")
                tid = obj.get("task_id")
                if rid and not req_id:
                    req_id = rid
                    print(f"  + requirement_id = {req_id}")
                if tid and not task_id:
                    task_id = tid
                    print(f"  + task_id        = {task_id}")
                # 进度
                if evt:
                    prog = obj.get("progress") or ""
                    if prog:
                        prog = str(prog)[:60]
                    print(f"    {evt:25s} {prog}")
                if evt in ("completed", "analysis_completed", "done"):
                    print("  ✓ 流水线完成")
                    break
except requests.exceptions.ConnectionError as e:
    print(f"  ✗ 连接失败: {e}")
    sys.exit(1)

if not req_id:
    print("  ⚠ 未拿到 requirement_id，无法继续验证。可能需要登录 token。")
    print("  请在前端手动创建一个测试需求，然后用下面的 SQL 验证：")
    print("""
      SELECT id, name, module_id, modules
      FROM functional_points ORDER BY created_at DESC LIMIT 10;
      -- 检查 search_vec（INSERT 后应自动填充，非空）
      SELECT id, title, search_vec IS NULL AS sv_null, ts_rank(search_vec, plainto_tsquery('zhparser','登录'))
      FROM requirements ORDER BY created_at DESC LIMIT 5;
    """)
    sys.exit(0)

# ---------------------------------------------------------------------------
# Step 4: 任务完成后查库验证 Bug A-1 + C-1
# ---------------------------------------------------------------------------
step(f"4. 等 task 完成，然后验证 A-1/C-1")

MAX_WAIT = 180  # 3 分钟
start = time.time()
status_final = None
while time.time() - start < MAX_WAIT:
    time.sleep(5)
    if task_id:
        r_task = S.get(f"{API_BASE}/tasks/{task_id}", timeout=10)
        if r_task.status_code == 200:
            tdata = r_task.json().get("data", {})
            st = tdata.get("status")
            print(f"  [{time.time()-start:.0f}s] task.status = {st}")
            if st in ("completed", "failed", "cancelled"):
                status_final = st
                break
    # 也可以直接查 requirement
    r_req = S.get(f"{API_BASE}/requirements/{req_id}", timeout=10)
    if r_req.status_code == 200:
        rdata = r_req.json().get("data", {})
        st = rdata.get("status")
        print(f"  [{time.time()-start:.0f}s] req.status = {st}")
        if st in ("confirmed", "draft", "pending_review", "failed"):
            status_final = st
            break
print(f"  最终状态: {status_final}")


async def verify_db(requirement_id):
    conn = await asyncpg.connect(DB_URL)
    try:
        # A-1: functional_points.module_id 填充
        print("\n  [A-1] 该需求下的 functional_points.module_id")
        fps = await conn.fetch(
            "SELECT id, name, suggested_module, module_id, modules FROM functional_points WHERE requirement_id = $1 ORDER BY idx",
            requirement_id,
        )
        ok_a1 = 0
        for f in fps:
            mid = f["module_id"] or "(空)"
            mods = json.dumps(f["modules"], ensure_ascii=False) if f["modules"] else "(空)"
            flag = "✓" if f["module_id"] else "✗"
            sug = (f["suggested_module"] or "")[:18]
            print(f"    {flag} {f['name'][:24]:24s}  sug={sug:18s}  module_id={mid:22s}  modules={mods}")
            if f["module_id"]:
                ok_a1 += 1
        total_fp = len(fps)
        print(f"  module_id 填充率 = {ok_a1}/{total_fp}")
        if total_fp == 0:
            print("  ⚠ 没有生成 FP（流水线可能未走完）")
        elif ok_a1 == total_fp:
            print("  ✓ A-1 修复生效：所有 FP 均写入 module_id / modules")
        else:
            print("  ⚠ A-1 部分生效，检查模块名称是否能在 module 表中精确匹配")

        # C-1: requirements.search_vec（触发器）
        print("\n  [C-1] requirements.search_vec")
        row = await conn.fetchrow(
            "SELECT id, title, search_vec IS NULL AS sv_null FROM requirements WHERE id = $1",
            requirement_id,
        )
        if row:
            sv_flag = "✗ NULL" if row["sv_null"] else "✓ 已填充"
            print(f"    {sv_flag} （触发器已自动维护 tsvector）")
            if not row["sv_null"]:
                print("  ✓ C-1 修复生效")
            else:
                print("  ⚠ C-1 修复未生效（触发器未执行？）")

        # B-1: 依赖图谱 include_inactive 默认 True（API 调用验证）
        print("\n  [B-1] 依赖图谱 include_inactive 默认值")
        if projs:
            proj_id = projs[0]["id"]
            r = S.get(f"{API_BASE}/products/{product_id}/projects/{proj_id}/fp-dependency-graph", timeout=10)
            if r.status_code == 200:
                data = r.json().get("data", {})
                edges = data.get("edges", []) or []
                inactive = [e for e in edges if e.get("is_active") is False]
                print(f"    API 返回总边数 = {len(edges)}, 其中 is_active=false 边数 = {len(inactive)}")
                if inactive:
                    print("  ✓ B-1 修复生效：默认返回归档冻结边（include_inactive=True）")
                else:
                    print("  ~ B-1 无冻结边，无法直接验证（需先归档项目）")
            else:
                print(f"    graph API 状态 = {r.status_code}，跳过")

        # C-3: modules 不为 NULL（DB 侧已默认 '[]'，代码侧也写了）
        print("\n  [C-3] functional_points.modules 类型检查")
        null_mods = await conn.fetchval(
            "SELECT COUNT(*) FROM functional_points WHERE requirement_id = $1 AND modules IS NULL",
            requirement_id,
        )
        print(f"    modules 为 NULL 的 FP = {null_mods}/{total_fp}")
        if null_mods == 0:
            print("  ✓ C-3 修复：modules 非 NULL，BM25 侧 json_array_elements_text 安全")

        print("\n" + "=" * 60)
        print("验证完成")
        print("=" * 60)
    finally:
        await conn.close()


asyncio.run(verify_db(req_id))

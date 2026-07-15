"""E2E 测试脚本：第一轮 + 第二轮全链路测试。

用法：python test_data/test_full_e2e.py
"""
import json
import time
import sys
import os
import httpx

BASE = "http://localhost:8000/api"
TIMEOUT = 600.0  # qwen3.7-plus 多次 LLM 调用需要较长超时

DOC1 = os.path.join(os.path.dirname(__file__), "requirements", "01_login_full.md")
DOC2 = os.path.join(os.path.dirname(__file__), "requirements", "02_login_optimize.md")


def log(step, msg, level="INFO"):
    print(f"  [{level}] [{step}] {msg}")


def sep(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


if __name__ == "__main__":
    import asyncio

    async def run():
        sep("E2E 测试开始")

        async with httpx.AsyncClient(base_url=BASE, timeout=TIMEOUT) as client:
            # ============================================================
            # Step 1: 登录
            # ============================================================
            sep("Step 1: 登录")
            resp = await client.post("/auth/login", json={"username": "admin", "password": "888888"})
            if resp.status_code != 200:
                log("1", f"登录失败: {resp.status_code} {resp.text[:200]}", "ERROR")
                sys.exit(1)
            token = resp.json()["data"]["token"]
            headers = {"Authorization": f"Bearer {token}"}
            log("1", "登录成功")

            # ============================================================
            # Step 2: 获取产品列表
            # ============================================================
            sep("Step 2: 获取产品列表")
            resp = await client.get("/products/products", headers=headers)
            products = resp.json().get("data", [])
            if not products:
                log("2", "无产品，创建新产品", "WARN")
                resp = await client.post("/products/products", json={
                    "name": f"E2E测试-{int(time.time())}",
                    "description": "E2E 全链路测试"
                }, headers=headers)
                pid = resp.json()["data"]["id"]
            else:
                pid = products[0]["id"]
            log("2", f"使用产品: {pid} ({products[0].get('name', '')})")

            # ============================================================
            # Step 3: 上传第一个文档（第一轮）
            # ============================================================
            sep("Step 3: 上传第一个文档 (01_login_full.md)")
            with open(DOC1, "r", encoding="utf-8") as f:
                content1 = f.read()
            log("3a", f"文档长度: {len(content1)}")

            resp = await client.post("/requirements/upload", json={
                "raw_text": content1,
                "product_id": pid,
                "source_type": "markdown",
            }, headers=headers)

            if resp.status_code != 200:
                log("3a", f"上传失败: {resp.status_code} {resp.text[:300]}", "ERROR")
                sys.exit(1)

            data = resp.json().get("data", {})
            if not data:
                log("3a", f"上传返回无 data: {resp.text[:300]}", "ERROR")
                sys.exit(1)

            req_id1 = data.get("req_id", "")
            srr1 = data.get("srr", {})
            log("3a", f"需求 ID: {req_id1}")
            log("3a", f"SRR 字段: {list(srr1.keys())[:15]}")
            print(f"  SRR requirement: {json.dumps(srr1.get('requirement', {}), ensure_ascii=False)[:200]}")

            # 保存 req_id 供后续使用
            with open(os.path.join(os.environ.get("TEMP", "/tmp"), "aitest_req1.txt"), "w") as f:
                f.write(req_id1)
            with open(os.path.join(os.environ.get("TEMP", "/tmp"), "aitest_pid.txt"), "w") as f:
                f.write(pid)

            # ============================================================
            # Step 4: 获取澄清问题
            # ============================================================
            sep("Step 4: 获取澄清问题")
            resp = await client.get(f"/requirements/{req_id1}/clarify/questions", headers=headers)
            if resp.status_code != 200:
                log("4", f"获取澄清失败: {resp.status_code} {resp.text[:200]}", "WARN")
            else:
                cdata = resp.json().get("data", {})
                questions = cdata.get("questions", [])
                quality = cdata.get("quality_report", {})
                kb_status = cdata.get("kb_status", {})
                log("4", f"问题数: {len(questions)}")
                log("4", f"质量评分: {quality.get('overall_score', 'N/A')}")
                log("4", f"KB 状态: empty={kb_status.get('kb_empty', 'N/A')}, first={kb_status.get('product_first_requirement', 'N/A')}")
                for i, q in enumerate(questions[:3]):
                    print(f"    Q{i+1}: [{q.get('priority', '?')}] {q.get('question', '')[:80]}")

            # ============================================================
            # Step 5: 提交澄清答案（空答案 + extra_notes）
            # ============================================================
            sep("Step 5: 提交澄清答案")
            answers = {}
            for q in questions:
                fid = q.get("id", "")
                answers[fid] = q.get("default_answer", "") or ""

            resp = await client.post(f"/requirements/{req_id1}/clarify", json={
                "answers": answers,
                "extra_notes": ""
            }, headers=headers)

            if resp.status_code != 200:
                log("5", f"提交澄清失败: {resp.status_code} {resp.text[:300]}", "WARN")
            else:
                cresp = resp.json()
                log("5", f"澄清提交: success={cresp.get('success')}, msg={cresp.get('message', '')[:100]}")

            # ============================================================
            # Step 6: 查询功能点列表
            # ============================================================
            sep("Step 6: 查询功能点列表")
            resp = await client.get(f"/requirements/{req_id1}/fp", headers=headers)
            if resp.status_code != 200:
                log("6", f"FP 查询失败: {resp.status_code} {resp.text[:200]}", "ERROR")
                fps = []
            else:
                fpdata = resp.json().get("data", {})
                fps = fpdata.get("items", []) if isinstance(fpdata, dict) else fpdata
                if not fps and isinstance(fpdata, dict):
                    fps = fpdata.get("functional_points", [])
                log("6", f"FP 数量: {len(fps)}")
                for i, fp in enumerate(fps[:8]):
                    print(f"    FP{i+1}: id={fp.get('id', '')[:20]} name={fp.get('name', '')[:30]} type={fp.get('fp_type', '')} priority={fp.get('priority', '')}")

            # 保存 FP IDs
            fp_ids = [fp.get("id", "") for fp in fps]
            with open(os.path.join(os.environ.get("TEMP", "/tmp"), "aitest_fp_ids.json"), "w") as f:
                json.dump(fp_ids, f)

            # ============================================================
            # Step 7: 添加 FP 依赖关系
            # ============================================================
            sep("Step 7: 添加 FP 依赖关系")
            deps_added = 0
            if len(fp_ids) >= 2:
                dep_specs = [
                    {"source_fp_id": fp_ids[0], "target_fp_id": fp_ids[1], "relation_type": "depends_on", "description": "FP1 依赖 FP2"},
                ]
                if len(fp_ids) >= 3:
                    dep_specs.append({"source_fp_id": fp_ids[0], "target_fp_id": fp_ids[2], "relation_type": "data_flows", "description": "FP1 数据流向 FP3"})
                if len(fp_ids) >= 4:
                    dep_specs.append({"source_fp_id": fp_ids[1], "target_fp_id": fp_ids[3] if len(fp_ids) > 3 else fp_ids[2], "relation_type": "conflicts_with", "description": "FP 与 FP 冲突"})

                for dep in dep_specs:
                    resp = await client.post(f"/requirements/{req_id1}/fp/dependencies", json=dep, headers=headers)
                    if resp.status_code == 200 and resp.json().get("success"):
                        deps_added += 1
                        log("7", f"依赖添加成功: {dep['relation_type']} {dep['source_fp_id'][:12]}→{dep['target_fp_id'][:12]}")
                    else:
                        log("7", f"依赖添加失败: {resp.status_code} {resp.text[:150]}", "WARN")
            else:
                log("7", f"FP 数量不足({len(fp_ids)})，跳过依赖添加", "WARN")
            log("7", f"共添加 {deps_added} 条依赖")

            # ============================================================
            # Step 8: 生成测试用例（SSE 流）
            # ============================================================
            sep("Step 8: 生成测试用例")
            log("8a", f"开始 SSE 生成: req_id={req_id1}")
            cases = []
            review_report = {}
            try:
                async with client.stream("POST", "/generate", json={"req_id": req_id1}, headers=headers, timeout=300.0) as sresp:
                    if sresp.status_code != 200:
                        body = await sresp.aread()
                        log("8a", f"生成失败: {sresp.status_code} {body[:300]}", "ERROR")
                    else:
                        async for line in sresp.aiter_lines():
                            if not line or not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if not payload:
                                continue
                            try:
                                evt = json.loads(payload)
                                stage = evt.get("stage", "")
                                progress = evt.get("progress", 0)
                                msg = evt.get("message", "")
                                print(f"    [{stage}] {progress}% - {msg[:100]}")
                                if evt.get("cases"):
                                    cases = evt["cases"]
                                if evt.get("review_report"):
                                    review_report = evt["review_report"]
                            except json.JSONDecodeError:
                                pass
            except Exception as e:
                log("8a", f"SSE 异常: {str(e)[:200]}", "ERROR")

            log("8b", f"生成用例数: {len(cases)}")
            if review_report:
                log("8b", f"审查报告: total={review_report.get('total_cases', 0)}, passed={review_report.get('passed_cases', 0)}, avg_conf={review_report.get('avg_confidence', 0):.2f}")
            for i, c in enumerate(cases[:5]):
                print(f"    TC{i+1}: {c.get('title', '')[:50]} [{c.get('type', '')}/{c.get('priority', '')}]")

            # ============================================================
            # Step 9: 归档需求
            # ============================================================
            sep("Step 9: 归档需求")
            go_live = time.strftime("%Y-%m-%d %H:%M:%S")
            resp = await client.post(f"/requirements/{req_id1}/archive", json={"go_live_date": go_live}, headers=headers)
            if resp.status_code != 200:
                log("9", f"归档失败: {resp.status_code} {resp.text[:300]}", "ERROR")
            else:
                adata = resp.json().get("data", {})
                log("9", f"归档成功: status={adata.get('status')}, kb_task_queued={adata.get('kb_task_queued')}")

            # ============================================================
            # Step 10: 验证 KB 查询
            # ============================================================
            sep("Step 10: 验证 KB 查询")
            log("10", "等待 8 秒让 Celery 完成向量化...")
            await asyncio.sleep(8)
            resp = await client.get("/knowledge", params={"q": "登录密码错误", "limit": 5}, headers=headers)
            if resp.status_code != 200:
                log("10", f"KB 查询失败: {resp.status_code} {resp.text[:200]}", "WARN")
            else:
                kdata = resp.json().get("data", {})
                if isinstance(kdata, list):
                    results = kdata
                elif isinstance(kdata, dict):
                    results = kdata.get("items", []) or kdata.get("results", [])
                else:
                    results = []
                log("10", f"KB 查询结果数: {len(results)}")
                for i, r in enumerate(results[:3]):
                    print(f"    KB{i+1}: {r.get('title', r.get('name', ''))[:40]} sim={r.get('similarity', 'N/A')}")

            # ============================================================
            # 第一轮总结
            # ============================================================
            sep("第一轮 E2E 测试完成")
            print(f"  需求 ID: {req_id1}")
            print(f"  FP 数量: {len(fps)}")
            print(f"  依赖数: {deps_added}")
            print(f"  用例数: {len(cases)}")
            print(f"  归档: {'成功' if resp.status_code == 200 else '失败'}")

            # 保存完整数据供第二轮使用
            e2e_data = {
                "req_id": req_id1,
                "product_id": pid,
                "fp_ids": fp_ids,
                "fps": [{"id": fp.get("id", ""), "name": fp.get("name", ""), "fp_type": fp.get("fp_type", ""), "priority": fp.get("priority", "")} for fp in fps],
                "cases_count": len(cases),
                "deps_added": deps_added,
                "archived": resp.status_code == 200,
            }
            data_path = os.path.join(os.environ.get("TEMP", "/tmp"), "aitest_e2e_data.json")
            with open(data_path, "w", encoding="utf-8") as f:
                json.dump(e2e_data, f, ensure_ascii=False, indent=2)
            log("SUMMARY", f"数据已保存到 {data_path}")

    asyncio.run(run())

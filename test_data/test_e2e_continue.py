"""E2E 测试脚本：从 FP 查询开始的后续步骤（步骤 4-10）。

用法：python test_data/test_e2e_continue.py <req_id>
"""
import json
import time
import sys
import os
import asyncio
import httpx

BASE = "http://localhost:8000/api"
TIMEOUT = 300.0  # 5 分钟超时


def log(step, msg, level="INFO"):
    print(f"  [{level}] [{step}] {msg}")


def sep(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


async def run():
    req_id = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("REQ_ID", "")
    if not req_id:
        print("用法: python test_e2e_continue.py <req_id>")
        sys.exit(1)

    sep(f"E2E 续测开始 | req_id={req_id}")

    async with httpx.AsyncClient(base_url=BASE, timeout=TIMEOUT) as client:
        # 登录
        resp = await client.post("/auth/login", json={"username": "admin", "password": "888888"})
        token = resp.json()["data"]["token"]
        headers = {"Authorization": f"Bearer {token}"}
        log("0", "登录成功")

        # 获取需求详情确认
        resp = await client.get(f"/requirements/{req_id}", headers=headers)
        if resp.status_code != 200:
            log("0", f"需求不存在: {resp.status_code}", "ERROR")
            sys.exit(1)
        detail = resp.json().get("data", {})
        log("0", f"需求: {detail.get('title', '')[:40]}, status={detail.get('status', '')}")
        pid = detail.get("product_id", "")

        # ============================================================
        # Step 4: 获取澄清问题
        # ============================================================
        sep("Step 4: 获取澄清问题")
        resp = await client.get(f"/requirements/{req_id}/clarify/questions", headers=headers)
        if resp.status_code != 200:
            log("4", f"获取澄清失败: {resp.status_code} {resp.text[:200]}", "WARN")
        else:
            cdata = resp.json().get("data", {})
            questions = cdata.get("questions", [])
            quality = cdata.get("quality_report", {})
            log("4", f"问题数: {len(questions)}, 质量评分: {quality.get('overall_score', 'N/A')}")

        # ============================================================
        # Step 5: 查询功能点列表
        # ============================================================
        sep("Step 5: 查询功能点列表")
        resp = await client.get(f"/requirements/{req_id}/fp", headers=headers)
        if resp.status_code != 200:
            log("5", f"FP 查询失败: {resp.status_code} {resp.text[:200]}", "ERROR")
            fps = []
        else:
            fpdata = resp.json().get("data", {})
            fps = fpdata.get("items", []) if isinstance(fpdata, dict) else fpdata
            if not fps and isinstance(fpdata, dict):
                fps = fpdata.get("functional_points", [])
            log("5", f"FP 数量: {len(fps)}")
            for i, fp in enumerate(fps[:8]):
                print(f"    FP{i+1}: id={fp.get('id', '')[:25]} name={fp.get('name', '')[:30]} type={fp.get('fp_type', '')} priority={fp.get('priority', '')}")

        fp_ids = [fp.get("id", "") for fp in fps]

        # ============================================================
        # Step 6: 添加 FP 依赖关系
        # ============================================================
        sep("Step 6: 添加 FP 依赖关系")
        deps_added = 0
        if len(fp_ids) >= 2:
            dep_specs = [
                {"source_fp_id": fp_ids[0], "target_fp_id": fp_ids[1], "relation_type": "depends_on", "description": "FP1 依赖 FP2 的功能"},
            ]
            if len(fp_ids) >= 3:
                dep_specs.append({"source_fp_id": fp_ids[0], "target_fp_id": fp_ids[2], "relation_type": "data_flows", "description": "FP1 数据流向 FP3"})
            if len(fp_ids) >= 2:
                dep_specs.append({"source_fp_id": fp_ids[1], "target_fp_id": fp_ids[0], "relation_type": "conflicts_with", "description": "FP2 与 FP1 存在冲突场景"})

            for dep in dep_specs:
                resp = await client.post(f"/requirements/{req_id}/fp/dependencies", json=dep, headers=headers)
                if resp.status_code == 200 and resp.json().get("success"):
                    deps_added += 1
                    log("6", f"依赖添加成功: {dep['relation_type']} {dep['source_fp_id'][:12]}→{dep['target_fp_id'][:12]}")
                else:
                    log("6", f"依赖添加失败: {resp.status_code} {resp.text[:150]}", "WARN")
        else:
            log("6", f"FP 数量不足({len(fp_ids)})，跳过依赖添加", "WARN")
        log("6", f"共添加 {deps_added} 条依赖")

        # ============================================================
        # Step 7: 生成测试用例（SSE 流）
        # ============================================================
        sep("Step 7: 生成测试用例")
        log("7a", f"开始 SSE 生成: req_id={req_id}")
        cases = []
        review_report = {}
        try:
            async with client.stream("POST", "/generate", json={"req_id": req_id}, headers=headers, timeout=600.0) as sresp:
                if sresp.status_code != 200:
                    body = await sresp.aread()
                    log("7a", f"生成失败: {sresp.status_code} {body[:300]}", "ERROR")
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
            log("7a", f"SSE 异常: {str(e)[:200]}", "ERROR")

        log("7b", f"生成用例数: {len(cases)}")
        if review_report:
            log("7b", f"审查报告: total={review_report.get('total_cases', 0)}, passed={review_report.get('passed_cases', 0)}, avg_conf={review_report.get('avg_confidence', 0):.2f}")
        for i, c in enumerate(cases[:5]):
            print(f"    TC{i+1}: {c.get('title', '')[:50]} [{c.get('type', '')}/{c.get('priority', '')}]")

        # ============================================================
        # Step 8: 归档需求
        # ============================================================
        sep("Step 8: 归档需求")
        go_live = time.strftime("%Y-%m-%d %H:%M:%S")
        resp = await client.post(f"/requirements/{req_id}/archive", json={"go_live_date": go_live}, headers=headers)
        if resp.status_code != 200:
            log("8", f"归档失败: {resp.status_code} {resp.text[:300]}", "ERROR")
        else:
            adata = resp.json().get("data", {})
            log("8", f"归档成功: status={adata.get('status')}, kb_task_queued={adata.get('kb_task_queued')}")

        # ============================================================
        # Step 9: 验证 KB 查询
        # ============================================================
        sep("Step 9: 验证 KB 查询")
        log("9", "等待 10 秒让 Celery 完成向量化...")
        await asyncio.sleep(10)
        resp = await client.get("/knowledge", params={"q": "登录密码错误锁定", "limit": 5}, headers=headers)
        if resp.status_code != 200:
            log("9", f"KB 查询失败: {resp.status_code} {resp.text[:200]}", "WARN")
        else:
            kdata = resp.json().get("data", {})
            if isinstance(kdata, list):
                results = kdata
            elif isinstance(kdata, dict):
                results = kdata.get("items", []) or kdata.get("results", [])
            else:
                results = []
            log("9", f"KB 查询结果数: {len(results)}")
            for i, r in enumerate(results[:3]):
                print(f"    KB{i+1}: {r.get('title', r.get('name', ''))[:40]} sim={r.get('similarity', 'N/A')}")

        # ============================================================
        # 第一轮总结
        # ============================================================
        sep("第一轮 E2E 续测完成")
        print(f"  需求 ID: {req_id}")
        print(f"  FP 数量: {len(fps)}")
        print(f"  依赖数: {deps_added}")
        print(f"  用例数: {len(cases)}")
        print(f"  产品 ID: {pid}")

        # 保存数据供第二轮使用
        with open(os.path.join(os.environ.get("TEMP", "/tmp"), "aitest_e2e_data.json"), "w") as f:
            json.dump({
                "req_id": req_id,
                "product_id": pid,
                "fp_ids": fp_ids,
                "cases_count": len(cases),
                "deps_added": deps_added,
            }, f, ensure_ascii=False)


if __name__ == "__main__":
    asyncio.run(run())

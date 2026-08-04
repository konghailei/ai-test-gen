"""E2E 全链路测试：上传 → 分析 → 归档 → 知识问答 → 增量上传（KB 补全）。

测试流程：
  1. 登录获取 token
  2. 创建产品
  3. 上传第一个文档（01_login_full.md）→ 检查每个链路节点
  4. 归档第一个文档 → 验证 KB 写入
  5. 知识问答 → 验证能回答归档需求的内容
  6. 上传第二个文档（02_login_optimize.md）→ 验证 KB 补全被调用
"""

import asyncio
import json
import time
import httpx
import sys
import os

BASE = "http://localhost:8000/api"
TIMEOUT = 120.0

# 测试文档路径
DOC1 = os.path.join(os.path.dirname(__file__), "..", "test_data", "requirements", "01_login_full.md")
DOC2 = os.path.join(os.path.dirname(__file__), "..", "test_data", "requirements", "02_login_optimize.md")


def log(step, msg, level="INFO"):
    prefix = f"[{level}]"
    print(f"  {prefix} [{step}] {msg}")


def sep(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


async def main():
    # 产品 ID 使用时间戳避免冲突
    product_id = f"PROD-E2E-{int(time.time())}"
    sep(f"E2E 测试开始 | product_id={product_id}")

    async with httpx.AsyncClient(base_url=BASE, timeout=TIMEOUT) as client:
        # ============================================================
        # Step 1: 登录
        # ============================================================
        sep("Step 1: 登录")
        resp = await client.post("/auth/login", json={
            "username": "admin",
            "password": "888888",
        })
        if resp.status_code != 200:
            log("1", f"登录失败: {resp.status_code} {resp.text[:200]}", "ERROR")
            sys.exit(1)
        token = resp.json()["data"]["token"]
        headers = {"Authorization": f"Bearer {token}"}
        log("1", "登录成功")

        # ============================================================
        # Step 2: 创建产品
        # ============================================================
        sep("Step 2: 创建产品")
        resp = await client.post("/products/products", json={
            "name": f"E2E测试产品-{product_id}",
            "description": "E2E 全链路测试",
        }, headers=headers)
        if resp.status_code == 200:
            pid = resp.json()["data"]["id"]
            log("2", f"产品创建成功: {pid}")
        else:
            log("2", f"产品创建失败: {resp.status_code} {resp.text[:100]}")
            # 尝试获取已有产品
            resp = await client.get("/products/products", headers=headers)
            products = resp.json().get("data", [])
            if products:
                pid = products[0]["id"]
                log("2", f"使用已有产品: {pid}")
            else:
                log("2", "无法获取产品", "ERROR")
                sys.exit(1)

        # ============================================================
        # Step 3: 上传第一个文档
        # ============================================================
        sep("Step 3: 上传第一个文档 (01_login_full.md)")
        with open(DOC1, "r", encoding="utf-8") as f:
            content1 = f.read()
        log("3a", f"文档内容长度: {len(content1)}")

        resp = await client.post("/requirements/upload", json={
            "raw_text": content1,
            "product_id": pid,
            "source_type": "markdown",
        }, headers=headers)

        if resp.status_code != 200:
            log("3a", f"上传失败: {resp.status_code} {resp.text[:300]}", "ERROR")
            sys.exit(1)

        data = resp.json()["data"]
        req_id_1 = data.get("req_id", "")
        log("3a", f"上传成功: req_id={req_id_1}")

        # 检查链路节点
        log("3b", f"状态: {data.get('status', '')}")
        log("3b", f"KB answered: {data.get('kb_answered', 0)}")
        log("3b", f"KB total: {data.get('kb_total', 0)}")
        log("3b", f"KB empty: {data.get('kb_empty', True)}")
        log("3b", f"Auto augmentations: {data.get('auto_augmentations_count', 0)}")
        log("3b", f"Has unanswered questions: {data.get('has_unanswered_questions', False)}")

        srr = data.get("srr", {})
        req_info = srr.get("requirement", {})
        log("3c", f"SRR 标题: {req_info.get('title', '')}")
        log("3c", f"SRR 模块: {req_info.get('modules', [])}")
        log("3c", f"SRR 优先级: {req_info.get('priority', '')}")

        flows = srr.get("flows", {})
        hp = flows.get("happy_path", {})
        hp_steps = hp.get("steps", [])
        log("3c", f"Happy path 步骤数: {len(hp_steps)}")

        af = flows.get("alternate_flows", [])
        log("3c", f"异常流程数: {len(af)}")

        ac = srr.get("acceptance_criteria", [])
        log("3c", f"验收标准数: {len(ac)}")

        br = srr.get("business_rules", [])
        log("3c", f"业务规则数: {len(br)}")

        # 检查 ClarifyAgent 状态
        clarify_state = srr.get("_clarify_state", {})
        if clarify_state:
            log("3d", f"Clarify status: {clarify_state.get('status', '')}")
            log("3d", f"Questions: {len(clarify_state.get('questions', []))}")
            log("3d", f"KB status: {json.dumps(clarify_state.get('kb_status', {}), ensure_ascii=False)}")
            log("3d", f"Auto augmentations: {len(clarify_state.get('auto_augmentations', []))}")
            log("3d", f"Field conflicts: {len(clarify_state.get('field_conflicts', []))}")

        # ============================================================
        # Step 4: 如果有澄清问题，需要先澄清或确认
        # ============================================================
        status = data.get("status", "")
        if status == "draft":
            sep("Step 4: 需求处于 draft 状态，执行 finalize")
            questions = clarify_state.get("questions", [])
            if questions:
                log("4", f"有 {len(questions)} 个澄清问题（完整文档预期 0-1 个）:")
                for q in questions:
                    log("4", f"  - [{q.get('priority','')}] {q.get('question','')[:80]}")

                # 如果有 KB 未解决问题，提交空答案直接 finalize
                answers = {}
                for q in questions:
                    answers[q.get("id", "")] = q.get("default_answer", "") or "无补充"

                resp = await client.post(f"/requirements/{req_id_1}/clarify", json={
                    "answers": answers,
                    "extra_notes": "",
                }, headers=headers)
                if resp.status_code == 200:
                    log("4", "澄清提交成功")
                    req_id_1 = resp.json()["data"].get("req_id", req_id_1)
                else:
                    log("4", f"澄清失败: {resp.status_code} {resp.text[:200]}", "ERROR")
            else:
                # 无问题，直接 finalize
                resp = await client.post(f"/requirements/{req_id_1}/finalize", headers=headers)
                if resp.status_code == 200:
                    log("4", "Finalize 成功")
                else:
                    log("4", f"Finalize 失败: {resp.status_code} {resp.text[:200]}", "WARN")
        else:
            sep("Step 4: 需求已为 active 状态（完整文档，无需澄清）")
            log("4", f"状态: {status}")

        # ============================================================
        # Step 5: 归档第一个文档
        # ============================================================
        sep("Step 5: 归档第一个文档")
        resp = await client.post(f"/requirements/{req_id_1}/archive", headers=headers)
        if resp.status_code != 200:
            log("5", f"归档失败: {resp.status_code} {resp.text[:300]}", "ERROR")
            sys.exit(1)

        archive_data = resp.json()["data"]
        log("5", f"归档状态: {archive_data.get('status', '')}")
        log("5", f"KB written: {archive_data.get('kb_task_queued', False)}")
        if archive_data.get("kb_error"):
            log("5", f"KB error: {archive_data.get('kb_error', '')}", "ERROR")

        if not archive_data.get("kb_task_queued"):
            log("5", "KB 写入失败，后续测试可能受影响", "WARN")

        # ============================================================
        # Step 6: 知识问答
        # ============================================================
        sep("Step 6: 知识问答（验证归档需求可被检索）")
        question = "登录功能连续失败多少次会锁定账号？锁定多长时间？"
        log("6", f"问题: {question}")

        # 知识问答是 SSE 流，需要用 stream
        async with client.stream("POST", "/knowledge-qa/ask", json={
            "question": question,
            "product_id": pid,
        }, headers=headers, timeout=TIMEOUT) as sresp:
            if sresp.status_code != 200:
                log("6", f"问答失败: {sresp.status_code}", "ERROR")
                async for chunk in sresp.aiter_text():
                    log("6", chunk[:200])
            else:
                sources = []
                answer = ""
                kb_status = {}
                event_type = ""
                async for line in sresp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("event:"):
                        event_type = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        payload = line.split(":", 1)[1].strip()
                        if event_type == "sources":
                            try:
                                sources = json.loads(payload)
                            except Exception:
                                pass
                        elif event_type == "chunk":
                            answer += payload
                        elif event_type == "done":
                            try:
                                done_data = json.loads(payload)
                                kb_status = done_data.get("kb_status", {})
                            except Exception:
                                pass

        log("6", f"检索到来源数: {len(sources)}")
        for s in sources[:3]:
            log("6", f"  - {s.get('title','')} (sim={s.get('similarity',0)})")
        log("6", f"KB status: {json.dumps(kb_status, ensure_ascii=False)}")
        log("6", f"回答前 200 字: {answer[:200]}")

        if len(sources) == 0:
            log("6", "未检索到来源！归档可能未生效", "WARN")
        else:
            log("6", "知识问答检索成功", "OK")

        # ============================================================
        # Step 7: 上传第二个文档（增量文档）
        # ============================================================
        sep("Step 7: 上传第二个文档 (02_login_optimize.md)")
        with open(DOC2, "r", encoding="utf-8") as f:
            content2 = f.read()
        log("7a", f"文档内容长度: {len(content2)}")

        resp = await client.post("/requirements/upload", json={
            "raw_text": content2,
            "product_id": pid,
            "source_type": "markdown",
        }, headers=headers)

        if resp.status_code != 200:
            log("7a", f"上传失败: {resp.status_code} {resp.text[:300]}", "ERROR")
            sys.exit(1)

        data2 = resp.json()["data"]
        req_id_2 = data2.get("req_id", "")
        log("7a", f"上传成功: req_id={req_id_2}")

        log("7b", f"状态: {data2.get('status', '')}")
        log("7b", f"KB answered: {data2.get('kb_answered', 0)}")
        log("7b", f"KB total: {data2.get('kb_total', 0)}")
        log("7b", f"KB empty: {data2.get('kb_empty', True)}")
        log("7b", f"Auto augmentations: {data2.get('auto_augmentations_count', 0)}")
        log("7b", f"Has unanswered questions: {data2.get('has_unanswered_questions', False)}")

        srr2 = data2.get("srr", {})
        clarify_state2 = srr2.get("_clarify_state", {})
        if clarify_state2:
            log("7c", f"Clarify status: {clarify_state2.get('status', '')}")
            log("7c", f"Questions: {len(clarify_state2.get('questions', []))}")
            log("7c", f"KB status: {json.dumps(clarify_state2.get('kb_status', {}), ensure_ascii=False)}")
            log("7c", f"Auto augmentations: {len(clarify_state2.get('auto_augmentations', []))}")
            log("7c", f"Field conflicts: {len(clarify_state2.get('field_conflicts', []))}")

            questions2 = clarify_state2.get("questions", [])
            if questions2:
                log("7d", f"澄清问题 ({len(questions2)} 个):")
                for q in questions2:
                    log("7d", f"  - [{q.get('priority','')}] {q.get('question','')[:80]}")

            # KB 补全信息
            augments2 = clarify_state2.get("auto_augmentations", [])
            if augments2:
                log("7e", f"KB 自动补全 ({len(augments2)} 项):")
                for aug in augments2:
                    val = str(aug.get("value", ""))[:100]
                    log("7e", f"  - {aug.get('field','')}: {val}")

        # ============================================================
        # 总结
        # ============================================================
        sep("E2E 测试总结")
        print(f"  产品 ID:        {pid}")
        print(f"  文档 1 req_id:  {req_id_1}")
        print(f"  文档 2 req_id:  {req_id_2}")
        print(f"  文档 1 状态:    archived (KB written={archive_data.get('kb_task_queued')})")
        print(f"  知识问答来源:   {len(sources)} 条")
        print(f"  文档 2 KB answered: {data2.get('kb_answered', 0)}")
        print(f"  文档 2 KB total:    {data2.get('kb_total', 0)}")
        print(f"  文档 2 KB augmentations: {data2.get('auto_augmentations_count', 0)}")

        kb_answered_2 = data2.get("kb_answered", 0)
        kb_total_2 = data2.get("kb_total", 0)
        if kb_total_2 > 0 and kb_answered_2 > 0:
            print(f"\n  ✅ KB 补全正常: {kb_answered_2}/{kb_total_2} 个问题被 KB 解答")
        elif kb_total_2 > 0 and kb_answered_2 == 0:
            print(f"\n  ⚠️ KB 补全异常: 生成了 {kb_total_2} 个 KB 查询但 0 个被解答")
        else:
            print(f"\n  ℹ️ 未生成 KB 查询（可能文档已足够完整）")


if __name__ == "__main__":
    asyncio.run(main())

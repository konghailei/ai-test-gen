"""测试澄清历史 + 重新生成功能。

流程：
  1. 上传第二个文档（增量文档）→ 有澄清问题 → 提交答案 → 检查历史
  2. 验证澄清历史 API 返回 clarify 记录
  3. 调用重新生成 API → 检查历史新增 regenerate 记录
"""

import asyncio
import json
import time
import httpx
import sys
import os

BASE = "http://localhost:8000/api"
TIMEOUT = 120.0

DOC1 = os.path.join(os.path.dirname(__file__), "..", "test_data", "requirements", "01_login_full.md")
DOC2 = os.path.join(os.path.dirname(__file__), "..", "test_data", "requirements", "02_login_optimize.md")


def log(step, msg, level="INFO"):
    print(f"  [{level}] [{step}] {msg}")


def sep(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


async def main():
    product_id = f"PROD-HIST-{int(time.time())}"
    sep(f"澄清历史 + 重新生成测试 | product_id={product_id}")

    async with httpx.AsyncClient(base_url=BASE, timeout=TIMEOUT) as client:
        # 登录
        resp = await client.post("/auth/login", json={"username": "admin", "password": "888888"})
        token = resp.json()["data"]["token"]
        headers = {"Authorization": f"Bearer {token}"}
        log("0", "登录成功")

        # 创建产品
        resp = await client.post("/products/products", json={
            "name": f"历史测试-{product_id}",
            "description": "澄清历史测试",
        }, headers=headers)
        pid = resp.json()["data"]["id"]
        log("0", f"产品: {pid}")

        # 上传第一个文档并归档
        with open(DOC1, "r", encoding="utf-8") as f:
            content1 = f.read()
        resp = await client.post("/requirements/upload", json={
            "raw_text": content1, "product_id": pid, "source_type": "markdown",
        }, headers=headers)
        req_id_1 = resp.json()["data"]["req_id"]
        log("1", f"文档1上传: {req_id_1}, status={resp.json()['data']['status']}")

        # 归档
        resp = await client.post(f"/requirements/{req_id_1}/archive", headers=headers)
        log("1", f"文档1归档: kb_written={resp.json()['data'].get('kb_written')}")

        # 上传第二个文档（增量）
        with open(DOC2, "r", encoding="utf-8") as f:
            content2 = f.read()
        resp = await client.post("/requirements/upload", json={
            "raw_text": content2, "product_id": pid, "source_type": "markdown",
        }, headers=headers)
        data2 = resp.json()["data"]
        req_id_2 = data2["req_id"]
        log("2", f"文档2上传: {req_id_2}, status={data2['status']}")
        log("2", f"KB answered={data2.get('kb_answered')}, total={data2.get('kb_total')}")
        log("2", f"Questions={data2.get('has_unanswered_questions')}")

        # 获取澄清问题
        resp = await client.get(f"/requirements/{req_id_2}/clarify/questions", headers=headers)
        clarify_data = resp.json()["data"]
        questions = clarify_data.get("questions", [])
        log("3", f"澄清问题数: {len(questions)}")

        if questions:
            # 提交澄清答案
            answers = {}
            for q in questions:
                answers[q["id"]] = q.get("default_answer", "") or "无补充"
            answers["extra_test"] = "test"

            resp = await client.post(f"/requirements/{req_id_2}/clarify", json={
                "answers": answers,
                "extra_notes": "测试澄清历史记录",
            }, headers=headers)
            log("4", f"澄清提交: status={resp.json()['data'].get('status')}")

        # 检查澄清历史
        sep("Step 5: 检查澄清历史")
        resp = await client.get(f"/requirements/{req_id_2}/clarify/history", headers=headers)
        hist_data = resp.json()["data"]
        log("5", f"历史记录数: {hist_data.get('total', 0)}")
        for entry in hist_data.get("items", []):
            action = entry.get("action", "")
            ts = entry.get("timestamp", "")[:19]
            if action == "clarify":
                q_count = len(entry.get("questions", []))
                a_count = len(entry.get("answers", {}))
                notes = entry.get("extra_notes", "")
                log("5", f"  [clarify] {ts} questions={q_count} answers={a_count} notes={notes[:30]}")
            elif action == "regenerate":
                notes = entry.get("extra_notes", "")
                prev = entry.get("previous_title", "")
                new = entry.get("new_title", "")
                log("5", f"  [regenerate] {ts} prev={prev[:30]} new={new[:30]} notes={notes[:30]}")

        # 测试重新生成
        sep("Step 6: 重新生成需求分析")
        regen_notes = "增加 OAuth2.0 第三方登录支持（Google/GitHub），用户可使用第三方账号登录"
        resp = await client.post(f"/requirements/{req_id_2}/regenerate", json={
            "extra_notes": regen_notes,
        }, headers=headers)

        if resp.status_code == 200:
            regen_data = resp.json()["data"]
            log("6", f"重新生成成功!")
            log("6", f"  旧标题: {regen_data.get('previous_title', '')[:50]}")
            log("6", f"  新标题: {regen_data.get('new_title', '')[:50]}")
            log("6", f"  状态: {regen_data.get('status', '')}")
        else:
            log("6", f"重新生成失败: {resp.status_code} {resp.text[:200]}", "ERROR")

        # 再次检查历史
        sep("Step 7: 再次检查澄清历史")
        resp = await client.get(f"/requirements/{req_id_2}/clarify/history", headers=headers)
        hist_data = resp.json()["data"]
        log("7", f"历史记录数: {hist_data.get('total', 0)}")
        for entry in hist_data.get("items", []):
            action = entry.get("action", "")
            ts = entry.get("timestamp", "")[:19]
            if action == "clarify":
                q_count = len(entry.get("questions", []))
                log("7", f"  [clarify] {ts} questions={q_count}")
            elif action == "regenerate":
                notes = entry.get("extra_notes", "")
                prev = entry.get("previous_title", "")
                new = entry.get("new_title", "")
                log("7", f"  [regenerate] {ts} prev={prev[:30]} new={new[:30]}")
                log("7", f"    notes: {notes[:60]}")

        # 总结
        sep("测试总结")
        total = hist_data.get("total", 0)
        clarify_count = sum(1 for e in hist_data.get("items", []) if e.get("action") == "clarify")
        regen_count = sum(1 for e in hist_data.get("items", []) if e.get("action") == "regenerate")
        print(f"  总历史记录: {total}")
        print(f"  澄清记录:   {clarify_count}")
        print(f"  重新生成:   {regen_count}")
        if clarify_count > 0 and regen_count > 0:
            print(f"\n  ✅ 澄清历史 + 重新生成功能正常")
        else:
            print(f"\n  ⚠️ 功能异常: clarify={clarify_count} regen={regen_count}")


if __name__ == "__main__":
    asyncio.run(main())

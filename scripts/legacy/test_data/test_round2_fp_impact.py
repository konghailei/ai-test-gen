"""第二轮 E2E 测试脚本：优化需求 + FP 影响分析。

用法：python test_data/test_round2_fp_impact.py

读取第一轮数据从 $TEMP/aitest_e2e_data.json
"""
import json
import time
import sys
import os
import asyncio
import httpx

BASE = "http://localhost:8000/api"
TIMEOUT = 600.0

DOC2 = os.path.join(os.path.dirname(__file__), "requirements", "02_login_optimize.md")
DATA_PATH = os.path.join(os.environ.get("TEMP", "/tmp"), "aitest_e2e_data.json")


def log(step, msg, level="INFO"):
    print(f"  [{level}] [{step}] {msg}")


def sep(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


async def run():
    # 加载第一轮数据
    if not os.path.exists(DATA_PATH):
        print(f"ERROR: 第一轮数据文件不存在 {DATA_PATH}")
        sys.exit(1)
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        round1 = json.load(f)

    pid = round1["product_id"]
    round1_fp_ids = round1.get("fp_ids", [])
    round1_req_id = round1["req_id"]

    sep(f"第二轮 E2E 测试 | product_id={pid}")
    print(f"  第一轮 req_id: {round1_req_id}")
    print(f"  第一轮 FP IDs: {round1_fp_ids}")

    async with httpx.AsyncClient(base_url=BASE, timeout=TIMEOUT) as client:
        # 登录
        resp = await client.post("/auth/login", json={"username": "admin", "password": "888888"})
        token = resp.json()["data"]["token"]
        headers = {"Authorization": f"Bearer {token}"}
        log("0", "登录成功")

        # ============================================================
        # Step 4.1: 上传优化需求文档
        # ============================================================
        sep("Step 4.1: 上传优化需求文档 (02_login_optimize.md)")
        with open(DOC2, "r", encoding="utf-8") as f:
            content2 = f.read()
        log("4.1", f"文档长度: {len(content2)}")

        resp = await client.post("/requirements/upload", json={
            "raw_text": content2,
            "product_id": pid,
            "source_type": "markdown",
        }, headers=headers)

        if resp.status_code != 200:
            log("4.1", f"上传失败: {resp.status_code} {resp.text[:300]}", "ERROR")
            sys.exit(1)

        data = resp.json().get("data", {})
        if not data:
            log("4.1", f"上传返回无 data: {resp.text[:300]}", "ERROR")
            sys.exit(1)

        req_id2 = data.get("req_id", "")
        srr2 = data.get("srr", {})
        log("4.1", f"新需求 ID: {req_id2}")
        log("4.1", f"SRR 字段: {list(srr2.keys())[:20]}")

        # 检查 KB 补全标记
        is_incremental = srr2.get("_is_incremental", False)
        baseline_gaps = srr2.get("_baseline_gaps", [])
        log("4.1", f"_is_incremental: {is_incremental}")
        log("4.1", f"_baseline_gaps: {len(baseline_gaps)} 项")

        # ============================================================
        # Step 4.2: 验证 KB 补全
        # ============================================================
        sep("Step 4.2: 验证 KB 补全")
        resp = await client.get(f"/requirements/{req_id2}/clarify/questions", headers=headers)
        if resp.status_code != 200:
            log("4.2", f"获取失败: {resp.status_code} {resp.text[:200]}", "WARN")
        else:
            cdata = resp.json().get("data", {})
            questions = cdata.get("questions", [])
            kb_status = cdata.get("kb_status", {})
            auto_augs = cdata.get("auto_augmentations", [])
            log("4.2", f"问题数: {len(questions)}")
            log("4.2", f"KB empty: {kb_status.get('kb_empty', 'N/A')}")
            log("4.2", f"auto_augmentations: {len(auto_augs)} 项")
            for aug in auto_augs[:3]:
                print(f"    aug: {aug.get('field', '')} = {str(aug.get('value', ''))[:80]}")

        # ============================================================
        # Step 4.3: 查询新需求的 FP 列表
        # ============================================================
        sep("Step 4.3: 查询新需求 FP 列表")
        resp = await client.get(f"/requirements/{req_id2}/fp", headers=headers)
        if resp.status_code != 200:
            log("4.3", f"FP 查询失败: {resp.status_code} {resp.text[:200]}", "ERROR")
            fps2 = []
        else:
            fpdata = resp.json().get("data", {})
            fps2 = fpdata.get("items", []) if isinstance(fpdata, dict) else fpdata
            if not fps2 and isinstance(fpdata, dict):
                fps2 = fpdata.get("functional_points", [])
            log("4.3", f"FP 数量: {len(fps2)}")
            for i, fp in enumerate(fps2[:8]):
                print(f"    FP{i+1}: id={fp.get('id', '')[:25]} name={fp.get('name', '')[:30]} type={fp.get('fp_type', '')} module_id={fp.get('module_id', '')}")

        fp_ids2 = [fp.get("id", "") for fp in fps2]

        # ============================================================
        # Step 4.3b: 添加跨需求 FP 依赖（为影响分析准备）
        # ============================================================
        sep("Step 4.3b: 添加跨需求 FP 依赖")
        deps_added = 0
        if fp_ids2 and round1_fp_ids:
            # 新需求的 FP 依赖第一轮的 FP（跨需求）
            dep_spec = {
                "source_fp_id": fp_ids2[0],
                "target_fp_id": round1_fp_ids[0],
                "relation_type": "depends_on",
                "description": "验证码登录依赖于基础登录功能（跨需求依赖）",
            }
            resp = await client.post(f"/requirements/{req_id2}/fp/dependencies", json=dep_spec, headers=headers)
            if resp.status_code == 200 and resp.json().get("success"):
                deps_added += 1
                log("4.3b", f"跨需求依赖添加成功: {fp_ids2[0][:12]} → {round1_fp_ids[0][:12]}")
            else:
                log("4.3b", f"跨需求依赖添加失败: {resp.status_code} {resp.text[:200]}", "WARN")

            # 再添加一条反向 data_flows
            if len(round1_fp_ids) >= 1:
                dep_spec2 = {
                    "source_fp_id": round1_fp_ids[0],
                    "target_fp_id": fp_ids2[0],
                    "relation_type": "data_flows",
                    "description": "基础登录数据流向验证码登录（会话传递）",
                }
                resp = await client.post(f"/requirements/{round1_req_id}/fp/dependencies", json=dep_spec2, headers=headers)
                if resp.status_code == 200 and resp.json().get("success"):
                    deps_added += 1
                    log("4.3b", f"反向依赖添加成功: {round1_fp_ids[0][:12]} → {fp_ids2[0][:12]} (data_flows)")
                else:
                    log("4.3b", f"反向依赖添加失败: {resp.status_code} {resp.text[:200]}", "WARN")
        else:
            log("4.3b", "无可用 FP，跳过依赖添加", "WARN")
        log("4.3b", f"共添加 {deps_added} 条跨需求依赖")

        # ============================================================
        # Step 4.4: FP 影响功能说明 — 自动生成（核心新功能）
        # ============================================================
        sep("Step 4.4: FP 影响说明自动生成 (核心新功能)")
        if not fp_ids2:
            log("4.4", "无 FP，跳过影响分析", "ERROR")
            sys.exit(1)

        target_fp_id = fp_ids2[0]
        log("4.4", f"对 FP {target_fp_id[:25]} 生成影响说明...")
        resp = await client.post(f"/requirements/{req_id2}/fp/{target_fp_id}/impact-description", headers=headers)

        if resp.status_code != 200:
            log("4.4", f"影响分析失败: {resp.status_code} {resp.text[:400]}", "ERROR")
            impact_result = {}
        else:
            rdata = resp.json()
            if not rdata.get("success"):
                log("4.4", f"影响分析 success=false: {rdata.get('message', '')}", "ERROR")
                impact_result = {}
            else:
                impact_result = rdata.get("data", {})
                log("4.4", f"影响分析成功!")
                log("4.4", f"  fp_id: {impact_result.get('fp_id', '')[:25]}")
                log("4.4", f"  fp_name: {impact_result.get('fp_name', '')}")
                log("4.4", f"  fp_type: {impact_result.get('fp_type', '')}")
                log("4.4", f"  module_id: {impact_result.get('module_id', '')}")
                log("4.4", f"  affected_fps: {len(impact_result.get('affected_fps', []))} 个")
                for afp in impact_result.get("affected_fps", [])[:3]:
                    print(f"    AFP: {afp.get('fp_name', '')} (relation={afp.get('relation_cn', '')}, depth={afp.get('depth', '')})")
                log("4.4", f"  affected_modules: {len(impact_result.get('affected_modules', []))} 个")
                for amod in impact_result.get("affected_modules", [])[:3]:
                    print(f"    AMOD: {amod.get('module_name', '')} (depth={amod.get('depth', '')})")
                log("4.4", f"  affected_requirements: {len(impact_result.get('affected_requirements', []))} 个")
                desc = impact_result.get("description", "")
                log("4.4", f"  description 长度: {len(desc)}")
                print(f"    description 片段: {desc[:200]}")
                tf = impact_result.get("test_focus", "")
                log("4.4", f"  test_focus 长度: {len(tf)}")
                print(f"    test_focus 片段: {tf[:200]}")
                log("4.4", f"  is_auto_generated: {impact_result.get('is_auto_generated', '')}")
                log("4.4", f"  generated_at: {impact_result.get('generated_at', '')}")

        # ============================================================
        # Step 4.5: 验证 SRR 中 impact_analysis 已写入
        # ============================================================
        sep("Step 4.5: 验证 SRR impact_analysis 写入")
        resp = await client.get(f"/requirements/{req_id2}", headers=headers)
        if resp.status_code != 200:
            log("4.5", f"查询需求失败: {resp.status_code}", "ERROR")
        else:
            detail = resp.json().get("data", {})
            srr_json = detail.get("srr_json", {})
            impact_list = srr_json.get("impact_analysis", [])
            log("4.5", f"impact_analysis 数组长度: {len(impact_list)}")
            for item in impact_list:
                log("4.5", f"  fp_id={item.get('fp_id', '')[:25]}, is_auto={item.get('is_auto_generated', '')}")
                log("4.5", f"  description 长度: {len(item.get('description', ''))}")

            # 验证目标 FP 在 impact_analysis 中
            found = any(item.get("fp_id") == target_fp_id for item in impact_list)
            if found:
                log("4.5", f"✅ 目标 FP {target_fp_id[:25]} 已在 impact_analysis 中")
            else:
                log("4.5", f"❌ 目标 FP 未在 impact_analysis 中找到", "ERROR")

        # ============================================================
        # Step 4.6: FP 影响说明 — 编辑保存（核心新功能）
        # ============================================================
        sep("Step 4.6: FP 影响说明编辑保存 (核心新功能)")
        edit_desc = "手动编辑的影响说明：登录验证码功能变更会影响会话管理和账号锁定逻辑，回归测试需覆盖验证码错误不增加锁定计数的场景。"
        edit_focus = "回归测试关注点：1. 验证码错误不触发账号锁定；2. 验证码过期后会话状态保持；3. 并发场景下验证码一致性"
        resp = await client.put(f"/requirements/{req_id2}/impact-description/{target_fp_id}", json={
            "description": edit_desc,
            "test_focus": edit_focus,
        }, headers=headers)

        if resp.status_code != 200:
            log("4.6", f"编辑保存失败: {resp.status_code} {resp.text[:300]}", "ERROR")
        else:
            log("4.6", f"编辑保存成功: {resp.json().get('message', '')}")

            # 再次查询验证更新
            resp = await client.get(f"/requirements/{req_id2}", headers=headers)
            if resp.status_code == 200:
                detail = resp.json().get("data", {})
                srr_json = detail.get("srr_json", {})
                impact_list = srr_json.get("impact_analysis", [])
                for item in impact_list:
                    if item.get("fp_id") == target_fp_id:
                        log("4.6", f"  验证 description 已更新: {item.get('description', '')[:80]}")
                        log("4.6", f"  验证 test_focus 已更新: {item.get('test_focus', '')[:80]}")
                        log("4.6", f"  验证 is_auto_generated: {item.get('is_auto_generated', '')}")
                        if item.get("is_auto_generated") == False:
                            log("4.6", "✅ is_auto_generated=False (标记为手动编辑)")
                        else:
                            log("4.6", "❌ is_auto_generated 未变为 False", "WARN")

        # ============================================================
        # Step 4.7: 生成测试用例（验证 impact_analysis 注入）
        # ============================================================
        sep("Step 4.7: 生成测试用例 (含 impact_analysis)")
        log("4.7a", f"开始 SSE 生成: req_id={req_id2}")
        cases = []
        review_report = {}
        try:
            async with client.stream("POST", "/generate", json={"req_id": req_id2}, headers=headers, timeout=600.0) as sresp:
                if sresp.status_code != 200:
                    body = await sresp.aread()
                    log("4.7a", f"生成失败: {sresp.status_code} {body[:300]}", "ERROR")
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
            log("4.7a", f"SSE 异常: {str(e)[:200]}", "ERROR")

        log("4.7b", f"生成用例数: {len(cases)}")
        if review_report:
            log("4.7b", f"审查报告: total={review_report.get('total_cases', 0)}, passed={review_report.get('passed_cases', 0)}, avg_conf={review_report.get('avg_confidence', 0):.2f}")
        for i, c in enumerate(cases[:5]):
            print(f"    TC{i+1}: {c.get('title', '')[:50]} [{c.get('type', '')}/{c.get('priority', '')}]")

        # ============================================================
        # Step 4.8: 验证 FP 依赖图谱端点
        # ============================================================
        sep("Step 4.8: 验证 FP 依赖图谱端点")
        resp = await client.get(f"/products/{pid}/fp/dependency-graph", headers=headers)
        if resp.status_code != 200:
            log("4.8", f"依赖图谱查询失败: {resp.status_code} {resp.text[:200]}", "ERROR")
        else:
            gdata = resp.json().get("data", {})
            nodes = gdata.get("nodes", [])
            edges = gdata.get("edges", [])
            log("4.8", f"FP 节点数: {len(nodes)}")
            log("4.8", f"FP 依赖边数: {len(edges)}")
            for node in nodes[:5]:
                print(f"    Node: id={node.get('id', '')[:20]} name={node.get('name', '')[:25]} type={node.get('fp_type', '')} req={node.get('req_id', '')[:20]}")
            for edge in edges[:5]:
                print(f"    Edge: {edge.get('source_fp_id', '')[:12]} → {edge.get('target_fp_id', '')[:12]} ({edge.get('relation_type', '')})")

        # ============================================================
        # 第二轮总结
        # ============================================================
        sep("第二轮 E2E 测试完成")
        print(f"  新需求 ID: {req_id2}")
        print(f"  新需求 FP 数量: {len(fps2)}")
        print(f"  跨需求依赖数: {deps_added}")
        print(f"  影响分析: {'✅ 生成成功' if impact_result else '❌ 失败'}")
        print(f"  影响分析 affected_fps: {len(impact_result.get('affected_fps', []))}")
        print(f"  影响分析 affected_modules: {len(impact_result.get('affected_modules', []))}")
        print(f"  用例数: {len(cases)}")
        print(f"  FP 图谱节点/边: {len(nodes) if resp.status_code == 200 else 0}/{len(edges) if resp.status_code == 200 else 0}")


if __name__ == "__main__":
    asyncio.run(run())

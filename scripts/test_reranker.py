"""测试 reranker 是否正常工作。"""
import requests
import json

API_BASE = "http://localhost:8000"

# 登录
login_resp = requests.post(
    f"{API_BASE}/api/auth/login",
    json={"username": "admin", "password": "888888"},
    timeout=15,
)
token = login_resp.json()["data"]["token"]
headers = {"Authorization": f"Bearer {token}"}

# 知识库问答（触发 hybrid_search + reranker）
question = "用户注册功能的密码规则是什么？"
print(f"[TEST] 问题: {question}")
print("[TEST] 发送 SSE 请求...")

resp = requests.post(
    f"{API_BASE}/api/knowledge-qa/ask",
    json={"question": question, "product_id": "PROD-DEFAULT"},
    headers=headers,
    stream=True,
    timeout=120,
)

print(f"[TEST] 响应状态码: {resp.status_code}")

current_event = ""
sources = []
answer_parts = []
done_data = None

for line in resp.iter_lines(decode_unicode=True):
    if not line:
        current_event = ""
        continue
    if line.startswith("event:"):
        current_event = line[6:].strip()
    elif line.startswith("data:"):
        raw_data = line[5:].strip()
        try:
            parsed = json.loads(raw_data)
        except json.JSONDecodeError:
            parsed = raw_data

        if current_event == "sources":
            if isinstance(parsed, list):
                sources = parsed
                print(f"[TEST] 收到 sources 事件: {len(sources)} 条来源")
                for i, s in enumerate(sources):
                    sim = s.get("similarity", 0)
                    title = s.get("title", "")[:60]
                    print(f"  来源 {i+1}: sim={sim} title={title}")
        elif current_event == "chunk":
            if isinstance(parsed, str):
                answer_parts.append(parsed)
            elif isinstance(parsed, dict):
                answer_parts.append(parsed.get("data", parsed.get("content", "")))
        elif current_event == "done":
            if isinstance(parsed, dict):
                done_data = parsed
                answer_parts.append(parsed.get("answer", ""))

print()
if answer_parts:
    answer = "".join(answer_parts)
    print(f"[TEST] 答案长度: {len(answer)}")
    print(f"[TEST] 答案前200字: {answer[:200]}")
else:
    print("[TEST] 无答案返回")

if done_data:
    kb_status = done_data.get("kb_status", {})
    print(f"\n[TEST] KB 状态: search_ok={kb_status.get('search_ok')}, total={kb_status.get('total')}")

"""XMind 思维导图格式导出器。

输出 .xmind 文件（ZIP 包，内含 content.json），兼容 XMind 8+。
测试用例按"需求 → 类型 → 用例"三级层次组织。
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import datetime
from typing import Any


def _make_topic(title: str, children: list[dict] | None = None) -> dict:
    """创建一个 XMind JSON topic 节点。"""
    topic: dict[str, Any] = {
        "id": uuid.uuid4().hex[:12],
        "class": "topic",
        "title": str(title),
    }
    if children:
        topic["children"] = {"attached": children}
    return topic


def export_to_xmind(cases: list[dict], filename: str = "testcases.xmind") -> bytes:
    """将测试用例导出为 XMind 思维导图。

    结构:
        测试用例（根）
          ├── {需求标题} (REQ-xxx)
          │   ├── [{类型}] 用例标题一
          │   ├── [{类型}] 用例标题二
          │   └── ...
          └── {需求标题} (REQ-xxx)
              └── ...

    Args:
        cases: UTCF 用例列表，每项含 id/title/type/req_id/referenced_requirement/case_json
        filename: 输出文件名

    Returns:
        bytes: .xmind ZIP 文件的字节内容
    """
    # 按需求分组
    groups: dict[str, dict] = {}
    for c in cases:
        req_id = c.get("req_id", "") or c.get("referenced_requirement", "") or "未知需求"
        req_title = c.get("req_title", "") or f"需求 {req_id}"
        if req_id not in groups:
            groups[req_id] = {"title": req_title, "cases": []}
        groups[req_id]["cases"].append(c)

    # 构建 XMind topic 树
    req_topics = []
    for req_id, g in sorted(groups.items()):
        case_children = []
        for c in g["cases"]:
            case_type = c.get("type", "用例")
            case_title = c.get("title", "未命名")
            case_node = _make_topic(f"[{case_type}] {case_title}")
            case_children.append(case_node)
        req_node = _make_topic(f"{g['title']} ({req_id})", case_children)
        req_topics.append(req_node)

    root = _make_topic("测试用例", req_topics)

    # 构建 content.json（XMind 标准格式）
    sheet = {
        "id": uuid.uuid4().hex[:12],
        "class": "sheet",
        "title": "Sheet 1",
        "rootTopic": root,
    }

    content = [sheet]

    # 打包为 ZIP
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("content.json", json.dumps(content, ensure_ascii=False, indent=2))

        # manifest.json（XMind 必需要素）
        manifest = {
            "file-entries": {
                "content.json": {},
                "metadata.json": {},
            }
        }
        zf.writestr("manifest.json", json.dumps(manifest))

        # metadata.json
        metadata = {
            "creator": {"name": "AI-Test-Gen", "version": "1.0"},
            "created": int(datetime.now().timestamp() * 1000),
        }
        zf.writestr("metadata.json", json.dumps(metadata))

    return buf.getvalue()

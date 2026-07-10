"""提示词持久化存储 — 基于 SQLite 的简单 key-value store。

每个模块的每个提示词标识为一个 key（如 "req_agent.SYSTEM_PROMPT"），
value 为提示词文本。default_value 保存初始默认值，支持一键恢复。
"""

from __future__ import annotations

import sqlite3
import os
from datetime import datetime

# 提示词持久化目录：与 aitestgen.db / Chroma 同处 /app/data（容器内映射到
# 宿主机 ./data，已绑定挂载，可持久化）。
# __file__ = /app/app/db/prompt_store.py → 上两层到 /app → /app/data/prompts.db
# ⚠️ 注意：原先写成上三层（落到容器根 /data），未挂载，容器重建即丢失，
#    导致管理页手动改过的提示词回退。现修正为 /app/data。
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "prompts.db")


def _get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_table():
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prompt_overrides (
            id TEXT PRIMARY KEY,
            module TEXT NOT NULL,
            name TEXT NOT NULL,
            value TEXT NOT NULL,
            default_value TEXT NOT NULL,
            description TEXT DEFAULT '',
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


# 启动时初始化
_init_table()


def get_all_prompts() -> list[dict]:
    """获取所有提示词（含覆盖状态）。"""
    _init_table()
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, module, name, value, default_value, description, updated_at FROM prompt_overrides ORDER BY module, name"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_prompt(module: str, name: str) -> str | None:
    """获取指定提示词的当前值（有覆盖返回覆盖值，否则返回 None 表示用默认）。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT value FROM prompt_overrides WHERE id = ?", (f"{module}.{name}",)
    ).fetchone()
    conn.close()
    return row["value"] if row else None


def get_prompt_default(module: str, name: str) -> str | None:
    """获取默认值。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT default_value FROM prompt_overrides WHERE id = ?", (f"{module}.{name}",)
    ).fetchone()
    conn.close()
    return row["default_value"] if row else None


def register_default(
    module: str,
    name: str,
    default_value: str,
    description: str = "",
) -> None:
    """注册/更新默认值（不会覆盖已有的用户自定义 value）。"""
    _init_table()
    conn = _get_conn()
    pid = f"{module}.{name}"
    existing = conn.execute("SELECT value, default_value FROM prompt_overrides WHERE id = ?", (pid,)).fetchone()

    if existing:
        # 已有记录：更新 default_value 和 description，但不覆盖用户自定义的 value
        if existing["value"] == existing["default_value"]:
            # 如果当前值等于旧默认值，同步更新为新默认值
            conn.execute(
                "UPDATE prompt_overrides SET default_value = ?, value = ?, description = ?, updated_at = ? WHERE id = ?",
                (default_value, default_value, description, datetime.now().isoformat(), pid),
            )
        else:
            # 用户已自定义，只更新 default 和 description
            conn.execute(
                "UPDATE prompt_overrides SET default_value = ?, description = ? WHERE id = ?",
                (default_value, description, pid),
            )
    else:
        conn.execute(
            "INSERT INTO prompt_overrides (id, module, name, value, default_value, description, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pid, module, name, default_value, default_value, description, datetime.now().isoformat()),
        )
    conn.commit()
    conn.close()


def update_prompt(module: str, name: str, value: str) -> None:
    """更新用户自定义的提示词。"""
    conn = _get_conn()
    pid = f"{module}.{name}"
    conn.execute(
        "UPDATE prompt_overrides SET value = ?, updated_at = ? WHERE id = ?",
        (value, datetime.now().isoformat(), pid),
    )
    conn.commit()
    conn.close()


def reset_prompt(module: str, name: str) -> None:
    """恢复默认值。"""
    conn = _get_conn()
    pid = f"{module}.{name}"
    conn.execute(
        "UPDATE prompt_overrides SET value = default_value, updated_at = ? WHERE id = ?",
        (datetime.now().isoformat(), pid),
    )
    conn.commit()
    conn.close()

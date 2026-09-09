# -*- coding: utf-8 -*-
"""任务环境上下文（P3-6 可观测性的地基，依据《深入理解 AI Agent》6.8）。

问题：Agent 内部的 llm_client 调用从不携带 task_id/req_id，llm_call_logs
3148 条全部无关联——trace 树断链。

方案：contextvars 环境上下文。编排器/异步任务入口 set 一次，该 asyncio
任务链内的所有 LLM 调用自动携带（llm_client 落库时兜底读取），
无需改动任何 Agent 的函数签名。
"""
from __future__ import annotations

from contextvars import ContextVar

current_task_id: ContextVar[str] = ContextVar("ambient_task_id", default="")
current_req_id: ContextVar[str] = ContextVar("ambient_req_id", default="")


def set_task_context(task_id: str = "", req_id: str = ""):
    """设置当前任务上下文，返回 (task_token, req_token) 供 reset 用。"""
    t_tok = current_task_id.set(task_id or "")
    r_tok = current_req_id.set(req_id or "")
    return t_tok, r_tok


def reset_task_context(tokens) -> None:
    try:
        t_tok, r_tok = tokens
        current_task_id.reset(t_tok)
        current_req_id.reset(r_tok)
    except Exception:
        pass


def get_task_id() -> str:
    return current_task_id.get()


def get_req_id() -> str:
    return current_req_id.get()

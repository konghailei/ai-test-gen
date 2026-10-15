"""去重 LLM 窄带裁决器（v3.5.10 docs/32 B-6 闸门2）。

职责单一：对"护栏全过 + sim≥0.92、即将删除的用例对"做单次轻量 LLM 判定，
兜住确定性词表的盲区（新角色名/英文角色/拼音缩写等未知家族）。

设计（docs/32 §B-6 分层闸门）：
- 只裁窄带：每批通常 0-5 对（只对"即将删除的对"触发，非全量比对）；
- fail-open 方向刻意选**保留**：LLM 失败/超时/解析失败 → 当作"非重复"，
  宁可漏删不可误删——去重是优化不是正确性，误删是不可逆事故；
- 同步桥接：dedup_semantic 跑在 asyncio.to_thread 工作线程里，llm_client
  是 async——make_sync_judge 用 run_coroutine_threadsafe 调度回主循环。

单测不依赖 LLM：dedup_semantic 的 llm_judge=None 时行为= 只有闸门1。
"""

from __future__ import annotations

import asyncio

from app.utils.logger import logger

# 单次裁决的轻量提示词：只问"是否同一测试场景"，角色/实体/操作不同即不是重复。
_JUDGE_SYSTEM = (
    "你是测试用例去重审核员。判断给出的两条测试用例是否为同一测试场景。"
    "注意：若两条用例的测试角色、业务实体、操作类型、预期分支任意一项不同，"
    "则它们不是重复用例（是刻意设计的变体），必须判为不同。"
    '只输出 JSON：{"is_duplicate": true} 或 {"is_duplicate": false}'
)


def _judge_prompt(a: dict, b: dict) -> str:
    def _brief(c: dict) -> str:
        steps = " / ".join(
            str(s.get("action", ""))[:60]
            for s in (c.get("steps") or []) if isinstance(s, dict)
        )[:300]
        return (
            f"标题: {str(c.get('title', ''))[:120]}\n"
            f"前置: {str(c.get('preconditions', ''))[:120]}\n"
            f"步骤: {steps}"
        )

    return f"【用例 A】\n{_brief(a)}\n\n【用例 B】\n{_brief(b)}\n\n是否同一测试场景？"


async def llm_dedup_judge(a: dict, b: dict) -> bool:
    """LLM 裁决两条用例是否重复（True=重复可合并）。

    任何失败（LLM 异常 / 解析失败 / 字段缺失）→ False（fail-open 保留）。
    """
    from app.core.llm_client import llm_client

    try:
        raw = await llm_client.call_json(
            prompt=_judge_prompt(a, b),
            system_prompt=_JUDGE_SYSTEM,
            temperature=0.0,
            agent_name="dedup_judge",
        )
        verdict = raw.get("is_duplicate") if isinstance(raw, dict) else None
        if not isinstance(verdict, bool):
            logger.warning("[DedupJudge] 裁决结果缺 is_duplicate 布尔，fail-open 保留")
            return False
        return verdict
    except Exception as e:  # noqa: BLE001
        logger.warning("[DedupJudge] LLM 裁决异常，fail-open 保留: {}", str(e)[:150])
        return False


def make_sync_judge(loop: asyncio.AbstractEventLoop, timeout: float = 30.0):
    """把 async 裁决器包装成同步 callable（供 dedup_semantic 在工作线程内调用）。

    Args:
        loop: 主事件循环（调用方在 async 上下文 get_running_loop() 捕获）。
        timeout: 单次裁决超时；超时按 fail-open（返回 False）处理。
    """

    def _judge(a: dict, b: dict) -> bool:
        try:
            coro = llm_dedup_judge(a, b)
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            return bool(fut.result(timeout=timeout))
        except Exception as e:  # noqa: BLE001
            logger.warning("[DedupJudge] 同步桥接失败/超时，fail-open 保留: {}", str(e)[:150])
            return False

    return _judge


def collect_role_words(srr: dict) -> set[str]:
    """从 SRR user_roles 收集动态角色词（叠加 case_dedup 静态兜底词表）。

    口径与 coverage.py 一致：role.get("role") or role.get("name")。
    """
    words: set[str] = set()
    for role in (srr or {}).get("user_roles") or []:
        if isinstance(role, dict):
            name = str(role.get("role") or role.get("name") or "").strip()
        else:
            name = str(role).strip()
        if name:
            words.add(name)
    return words

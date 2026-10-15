"""知识问答提问长度上限单测（v4.0.1 / 缺陷 S8）。

需求（载体文档 §2.1 / §6）：提问长度上限 500 字，超出时前端禁止继续输入并提示。
前端 `KnowledgeQA.tsx` 用 `maxLength=500` 输入层拦截；
后端 `KbQAAskRequest` / `KbQACharRequest` 做**同口径二次防御**——
绕过前端直调 API 也必须被拒绝（否则超长输入会进入检索与 Prompt 构造链路）。

守卫目标：把上限锁在 500（含边界 500 通过 / 501 拒绝），防止被无意放宽。
"""
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent.parent))  # 支持 python tests/xxx.py 直跑

from app.models.api_schemas import (
    KB_QA_MAX_QUESTION_LEN,
    KbQAAskRequest,
    KbQACharRequest,
)


def test_limit_is_500():
    assert KB_QA_MAX_QUESTION_LEN == 500


def test_ask_accepts_boundary_500():
    req = KbQAAskRequest(question="字" * 500)
    assert len(req.question) == 500


def test_ask_rejects_501():
    with pytest.raises(ValidationError):
        KbQAAskRequest(question="字" * 501)


def test_ask_rejects_empty():
    with pytest.raises(ValidationError):
        KbQAAskRequest(question="")


def test_chat_accepts_boundary_500():
    req = KbQACharRequest(record_id="KQA-1", question="字" * 500)
    assert len(req.question) == 500


def test_chat_rejects_501():
    with pytest.raises(ValidationError):
        KbQACharRequest(record_id="KQA-1", question="字" * 501)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

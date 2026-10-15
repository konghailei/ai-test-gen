"""敏感信息脱敏单测（v4.0.1 / 缺陷 S9）。

需求：检索结果中的敏感信息（密钥、token）必须脱敏后展示。
实测泄露样本为手机号（TEST_PLAN §I #35），故 PII 一并覆盖。

守卫目标：
  1. 密钥/token 类（sk-*、Bearer、JWT、key=value）必须掩蔽；
  2. PII 类（手机号/身份证/邮箱）掩蔽中间段，保留可辨识的两端；
  3. 不误伤普通文本（版本号、需求 ID、长数字串）；
  4. `redact_sources` / `redact_claims` 只动展示字段，不破坏结构化字段。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # 支持 python tests/xxx.py 直跑

from app.utils.redact import redact, redact_claims, redact_sources


# ---------------------------------------------------------------- 密钥 / token

def test_redacts_openai_style_key():
    out = redact("请使用 sk-TESTKEY0123456789abcdef 调用")
    assert "sk-TESTKEY0123456789abcdef" not in out
    assert "sk-********" in out


def test_redacts_bearer_token():
    out = redact("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9abc")
    assert "eyJhbGciOiJIUzI1NiJ9abc" not in out
    assert "***" in out


def test_redacts_jwt():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefg"
    out = redact(f"token={jwt}")
    assert jwt not in out


def test_redacts_key_value_form():
    out = redact("api_key=ABCD1234EFGH secret_key: xyzw9876")
    assert "ABCD1234EFGH" not in out
    assert "xyzw9876" not in out


# ---------------------------------------------------------------------- PII

def test_redacts_cn_mobile_keeping_prefix_and_suffix():
    out = redact("联系客服 13800138000 处理")
    assert "138****8000" in out
    assert "13800138000" not in out


def test_redacts_email_local_part():
    out = redact("发送到 zhangsan@example.com")
    assert "zhangsan@" not in out
    assert "@example.com" in out


def test_redacts_id_card():
    out = redact("身份证 410101199001011234 已登记")
    assert "410101199001011234" not in out


# --------------------------------------------------------------- 不误伤

def test_keeps_long_requirement_id_untouched():
    """13 位需求 ID 不应被当作手机号掩蔽（?<!\\d)(?!\\d) 边界）。"""
    text = "REQ-1788322894962-6696 已归档"
    assert redact(text) == text


def test_keeps_version_number_untouched():
    text = "版本编号规则：v20260815001"
    assert redact(text) == text


def test_keeps_normal_text_untouched():
    text = "用户登录需要输入账号密码，连续失败 5 次锁定账号 1 分钟"
    assert redact(text) == text


def test_pii_can_be_disabled():
    out = redact("联系 13800138000", include_pii=False)
    assert "13800138000" in out


# --------------------------------------------------------------- 结构化字段

def test_redact_sources_only_touches_display_fields():
    src = [{
        "id": "sk-abcdefghijklmnop",       # 结构化字段：不脱敏（避免破坏契约）
        "type": "requirement",
        "similarity": 0.75,
        "title": "联系 13800138000 的需求",
        "summary": "密钥 sk-TESTKEY0123456789abcdef",
    }]
    out = redact_sources(src)
    assert out[0]["id"] == "sk-abcdefghijklmnop"
    assert out[0]["similarity"] == 0.75
    assert "13800138000" not in out[0]["title"]
    assert "sk-TESTKEY0123456789abcdef" not in out[0]["summary"]


def test_redact_claims_keeps_source_ids():
    claims = [{"claim": "手机号 13800138000 用于登录", "source_ids": ["1", "2"]}]
    out = redact_claims(claims)
    assert out[0]["source_ids"] == ["1", "2"]
    assert "13800138000" not in out[0]["claim"]


def test_redact_idempotent():
    once = redact("sk-TESTKEY0123456789abcdef 与 13800138000")
    twice = redact(once)
    assert once == twice


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))

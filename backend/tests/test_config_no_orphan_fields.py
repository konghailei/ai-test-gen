# -*- coding: utf-8 -*-
"""守卫：Settings 字段必须至少有一个真实消费点（v4.1.8）。

背景（2026-10-28 全仓审计）：config.py 曾有 13 个字段全仓零消费——其中 10 个
在 .env.example 有文档、compose 有注入，用户调了完全不生效（"假配置"）。
历史同类：MATCH_ENABLE_GRAPH（2026-10-22 删）、RERANKER_THRESHOLD（被
MATCH_FINAL_N 顶替，改它无效）。

判定口径：字段名必须作为**词元**出现在 backend/app（除 config.py）或
frontend/src 的源码里。本测试是**下限守卫**——纯文本扫描无法区分注释与代码，
拦得住"彻底没人提"（本次 13 项均属此类），拦不住"只在注释里提一句"；
后者靠评审纪律（加配置前先 grep 消费方）。

刻意不 import app：纯文件扫描，无第三方依赖，宿主机/容器均可直接跑。
"""
from __future__ import annotations

import re
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]  # backend/
_APP_DIR = _BACKEND / "app"
_CONFIG = _APP_DIR / "config.py"
_FRONTEND = _BACKEND.parent / "frontend" / "src"

# Settings 类字段（4 空格缩进 + 全大写名 + 类型标注冒号）
_FIELD_RE = re.compile(r"^\s{4}([A-Z][A-Z0-9_]*)\s*:", re.M)
_TOKEN_RE = re.compile(r"\w+")


def _settings_fields() -> set[str]:
    return set(_FIELD_RE.findall(_CONFIG.read_text(encoding="utf-8")))


def _corpus_tokens() -> set[str]:
    tokens: set[str] = set()
    for root, suffixes in ((_APP_DIR, (".py",)), (_FRONTEND, (".ts", ".tsx"))):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix not in suffixes or "__pycache__" in path.parts:
                continue
            if path == _CONFIG or "tests" in path.parts:
                continue
            try:
                tokens.update(_TOKEN_RE.findall(path.read_text(encoding="utf-8", errors="ignore")))
            except OSError:
                continue
    return tokens


def test_every_setting_has_consumer():
    fields = _settings_fields()
    assert fields, "未从 config.py 解析到任何 Settings 字段——正则或路径失效，测试本身有问题"
    tokens = _corpus_tokens()
    orphans = sorted(f for f in fields if f not in tokens)
    assert not orphans, (
        "以下 Settings 字段全仓零消费（假配置/死配置）——"
        "要么接线，要么删除并同步移除 .env.example 中的文档：\n  " + "\n  ".join(orphans)
    )

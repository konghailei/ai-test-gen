#!/usr/bin/env bash
# ============================================================================
# fetch-deps.sh — 薄包装：调用跨平台 Python 脚本 fetch-deps.py
# （Windows / Linux 逻辑统一在 fetch-deps.py，本文件不再维护独立逻辑）
#
# 在 Windows 上若用 Git Bash 运行本文件；也可直接用：
#   python scripts/fetch-deps.py  /  python3 scripts/fetch-deps.py
# ============================================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYBIN="$(command -v python3 || command -v python)"
exec "$PYBIN" "$SCRIPT_DIR/fetch-deps.py" "$@"

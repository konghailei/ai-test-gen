@echo off
chcp 65001 >nul 2>&1
:: ============================================================================
:: fetch-deps.bat — 薄包装：调用跨平台 Python 脚本 fetch-deps.py
:: （Windows / Linux 逻辑统一在 fetch-deps.py，本文件不再维护独立逻辑）
::
:: 直接运行即可，无需 bash / WSL：
::   scripts\fetch-deps.bat
:: ============================================================================
python "%~dp0fetch-deps.py" %*

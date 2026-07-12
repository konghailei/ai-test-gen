# AI-Test-Gen 一键脚本
#
# 使用方式：
#   Windows: scripts\setup.bat
#   Linux/Mac: bash scripts/setup.sh

@echo off
chcp 65001 >nul
title AI-Test-Gen 环境初始化

echo ============================================
echo  AI-Test-Gen 环境初始化
echo ============================================
echo.

:: 检查 Python 版本
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python，请安装 Python 3.11+
    pause
    exit /b 1
)

:: 检查 Python 版本 >= 3.11
python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"
if %errorlevel% neq 0 (
    echo [错误] 需要 Python 3.11+，当前版本:
    python --version
    pause
    exit /b 1
)

echo [1/4] 检查 .env 配置文件...
if not exist .env (
    echo [提示] 未找到 .env 文件，正在从 .env.example 复制...
    copy .env.example .env
    echo [注意] 请编辑 .env 文件填入实际的 API Key
)

:: 创建虚拟环境
echo [2/4] 创建虚拟环境...
if not exist venv (
    python -m venv venv
    echo [完成] 虚拟环境已创建
) else (
    echo [完成] 虚拟环境已存在，跳过
)

:: 激活虚拟环境并安装依赖
echo [3/4] 安装依赖...
call venv\Scripts\activate.bat
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [警告] 部分依赖安装失败，请检查网络
) else (
    echo [完成] 依赖安装完成
)

:: 校验 Milvus 依赖
echo [4/4] 校验 Milvus 依赖...
python -c "import pymilvus" 2>nul || echo 警告: pymilvus 未安装
echo [完成] 环境初始化完成

echo.
echo ============================================
echo  启动方式:
echo    call venv\Scripts\activate.bat
echo    uvicorn app.main:app --reload --port 8000
echo ============================================
pause

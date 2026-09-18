@echo off
chcp 65001 >nul
REM ============================================================
REM  启动控制面（开发适配器版）
REM
REM  前置：
REM    1. 已建虚拟环境：python -m venv .venv
REM    2. 已装依赖：    .venv\Scripts\python -m pip install -e ".[dev]"
REM    3. 已启动数据库：scripts\pg_start.cmd（当前版本尚未连接数据库，可选）
REM ============================================================

cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到 .venv，请先执行：
    echo     python -m venv .venv
    echo     .venv\Scripts\python -m pip install -e ".[dev]"
    exit /b 1
)

REM 会话签名密钥。生产必须由 KMS / Secret Manager 注入，不能这样明文设置。
if "%STUDY_PLATFORM_SESSION_SECRET%"=="" (
    set STUDY_PLATFORM_SESSION_SECRET=dev-only-session-secret-change-me
)

set PYTHONPATH=%CD%\backend

echo ============================================================
echo  控制面即将在 http://127.0.0.1:8000/ 启动
echo.
echo  浏览器打开首页后，需要先签发会话令牌（另开一个终端）：
echo    .venv\Scripts\python tools\issue_session.py --tenant tenant_demo --principal user_demo
echo.
echo  开箱可用的演示租户与项目：
echo    租户 tenant_demo / 主体 user_demo / 项目 proj_demo
echo ============================================================
echo.

.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

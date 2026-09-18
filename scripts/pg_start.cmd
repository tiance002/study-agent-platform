@echo off
chcp 65001 >nul
REM ============================================================
REM  启动 PostgreSQL 16.4（数据目录 E:\pgsql\data）
REM
REM  为什么需要这个脚本：
REM  由自动化工具启动的进程会在命令结束时被清理，无法常驻。
REM  数据库必须在**你自己的终端**里启动。
REM ============================================================

set PGBIN=E:\pgsql\bin
set PGDATA=E:\pgsql\data

if not exist "%PGBIN%\postgres.exe" (
    echo [错误] 找不到 PostgreSQL：%PGBIN%\postgres.exe
    exit /b 1
)

if exist "%PGDATA%\postmaster.pid" (
    echo [提示] 检测到 postmaster.pid，可能已在运行。
    echo        若确认未运行，请先删除：%PGDATA%\postmaster.pid
)

echo 正在启动 PostgreSQL...
"%PGBIN%\pg_ctl.exe" -D "%PGDATA%" -l "E:\pgsql\server.log" -w start
if errorlevel 1 (
    echo.
    echo [失败] 启动未成功。最近的日志：
    powershell -NoProfile -Command "Get-Content -Tail 15 'E:\pgsql\server.log'"
    exit /b 1
)

echo.
echo ============================================================
echo  启动成功
echo ============================================================
echo  数据库：   study_platform
echo  超级用户： postgres（本地 trust 认证，仅开发用）
echo  应用角色： study_app（NOSUPERUSER / NOBYPASSRLS —— RLS 生效的前提）
echo  连接串：   postgresql://study_app:dev-only-app-password@127.0.0.1:5432/study_platform
echo.
echo  停止：scripts\pg_stop.cmd
echo  日志：E:\pgsql\server.log

@echo off
chcp 65001 >nul
REM 停止 PostgreSQL（数据目录 E:\pgsql\data）

set PGBIN=E:\pgsql\bin
set PGDATA=E:\pgsql\data

echo 正在停止 PostgreSQL...
"%PGBIN%\pg_ctl.exe" -D "%PGDATA%" -m fast stop
if errorlevel 1 (
    echo [提示] 停止失败，可能本来就没在运行。
    exit /b 0
)
echo 已停止。

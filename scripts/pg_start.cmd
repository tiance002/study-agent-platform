@echo off
setlocal
REM ============================================================
REM  Start PostgreSQL 16.4   (data directory: E:\pgsql\data)
REM
REM  Why this file exists:
REM    Processes started by an automation tool are killed when the
REM    tool's command returns, so the database cannot stay resident.
REM    PostgreSQL must be started from YOUR OWN terminal.
REM
REM  Why there is no Chinese text in this file:
REM    cmd.exe parses .cmd files using the system code page (GBK on
REM    zh-CN Windows). A UTF-8 Chinese comment gets mis-decoded into
REM    garbage, the parser's read offset drifts, and cmd then tries to
REM    execute byte fragments as commands. Keep this file PURE ASCII.
REM    (Chinese instructions live in README.md instead.)
REM ============================================================

set "PGBIN=E:\pgsql\bin"
set "PGDATA=E:\pgsql\data"
set "PGLOG=E:\pgsql\server.log"

if not exist "%PGBIN%\postgres.exe" (
    echo [ERROR] PostgreSQL not found at %PGBIN%\postgres.exe
    exit /b 1
)

REM Exit code of "pg_ctl status": 0 = running, 3 = not running, 4 = bad data dir.
"%PGBIN%\pg_ctl.exe" -D "%PGDATA%" status >nul 2>&1
if not errorlevel 1 (
    echo [INFO] PostgreSQL is already running. Nothing to do.
    echo        Stop it with: scripts\pg_stop.cmd
    exit /b 0
)

if exist "%PGDATA%\postmaster.pid" (
    echo [WARN] Found a stale postmaster.pid while no server is running.
    echo        This is normal after an unclean shutdown; pg_ctl recovers
    echo        from it. If startup fails below, delete that file first.
    echo.
)

echo Starting PostgreSQL...
echo.
"%PGBIN%\pg_ctl.exe" -D "%PGDATA%" -l "%PGLOG%" -w start
if errorlevel 1 (
    echo.
    echo [FAILED] Startup did not succeed. Last lines of the log:
    echo ------------------------------------------------------------
    powershell -NoProfile -Command "Get-Content -Tail 20 '%PGLOG%'"
    echo ------------------------------------------------------------
    exit /b 1
)

echo.
echo ============================================================
echo   PostgreSQL is up
echo ============================================================
echo   Database  : study_platform
echo   Superuser : postgres   (local trust auth - development only)
echo   App role  : study_app  (NOSUPERUSER / NOBYPASSRLS - needed for RLS)
echo   DSN       : postgresql://study_app:dev-only-app-password@127.0.0.1:5432/study_platform
echo.
echo   Stop : scripts\pg_stop.cmd
echo   Log  : %PGLOG%
echo.
endlocal

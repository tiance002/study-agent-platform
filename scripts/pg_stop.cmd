@echo off
setlocal
REM ============================================================
REM  Stop PostgreSQL   (data directory: E:\pgsql\data)
REM  Pure ASCII on purpose - see the note in pg_start.cmd.
REM ============================================================

set "PGBIN=E:\pgsql\bin"
set "PGDATA=E:\pgsql\data"

"%PGBIN%\pg_ctl.exe" -D "%PGDATA%" status >nul 2>&1
if errorlevel 1 (
    echo [INFO] PostgreSQL is not running. Nothing to do.
    exit /b 0
)

echo Stopping PostgreSQL...
"%PGBIN%\pg_ctl.exe" -D "%PGDATA%" -m fast -w stop
if errorlevel 1 (
    echo [WARN] Stop did not succeed. Check the log: E:\pgsql\server.log
    exit /b 1
)

echo Done.
endlocal

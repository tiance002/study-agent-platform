@echo off
setlocal
REM ============================================================
REM  Local database self-check.
REM
REM  Answers four questions in one command:
REM    1. Is the local PostgreSQL reachable?
REM    2. Is the application role properly restricted?
REM       (NOSUPERUSER + NOBYPASSRLS - without this, RLS is theatre:
REM        the role simply ignores every policy.)
REM    3. Does row level security isolate tenants?
REM    4. Does a MISSING tenant context yield zero rows - not every row?
REM
REM  IMPORTANT: sections 4-6 connect as the APPLICATION role on purpose.
REM  A superuser bypasses row level security entirely, so checking
REM  isolation as postgres would prove nothing at all.
REM
REM  Requires the server to be running:  scripts\pg_start.cmd
REM  Pure ASCII on purpose - see the note in pg_start.cmd.
REM ============================================================

set "PGBIN=E:\pgsql\bin"
set "PGDATA=E:\pgsql\data"
set "PGHOST=127.0.0.1"
set "PGPORT=5432"
set "PGDB=study_platform"
set "PGAPP=study_app"
set "PGAPPPW=dev-only-app-password"
set "OUT=%TEMP%\db_check_out.txt"
set /a FAIL=0

if not exist "%PGBIN%\psql.exe" (
    echo [ERROR] psql not found at %PGBIN%\psql.exe
    exit /b 1
)

"%PGBIN%\pg_ctl.exe" -D "%PGDATA%" status >nul 2>&1
if errorlevel 1 (
    echo [ERROR] PostgreSQL is not running.
    echo         Start it first:  scripts\pg_start.cmd
    exit /b 1
)

REM Superuser connection: used only for role inspection and fixture loading.
set "SU=%PGBIN%\psql.exe -U postgres -h %PGHOST% -p %PGPORT% -d %PGDB% -t -A -q"
REM Application connection: used for every isolation check.
set "APP=%PGBIN%\psql.exe -U %PGAPP% -h %PGHOST% -p %PGPORT% -d %PGDB% -t -A -q"
set PGPASSWORD=%PGAPPPW%

echo ============================================================
echo  1) Server
echo ============================================================
%SU% -c "select 'version : ' || version();" > "%OUT%" 2>&1
if errorlevel 1 (
    echo [FAIL] cannot connect as postgres
    type "%OUT%"
    exit /b 1
)
type "%OUT%"

echo.
echo ============================================================
echo  2) Application role restrictions
echo ============================================================
%SU% -c "select case when rolsuper or rolbypassrls then 'FAIL - role can bypass RLS' else 'OK   - NOSUPERUSER + NOBYPASSRLS' end from pg_roles where rolname = '%PGAPP%';" > "%OUT%" 2>&1
type "%OUT%"
findstr /c:"FAIL" "%OUT%" >nul && set /a FAIL+=1

echo.
echo ============================================================
echo  3) Load RLS fixture
echo ============================================================
%SU% -f "%~dp0sql\rls_smoke.sql" > "%OUT%" 2>&1
findstr /c:"ERROR" "%OUT%" >nul && (
    echo [FAIL] fixture failed to load:
    type "%OUT%"
    exit /b 1
)
echo OK   - fixture loaded (demo_doc recreated for tenants t1 and t2)

echo.
echo ============================================================
echo  4) Tenant isolation (connected as %PGAPP%)
echo ============================================================
%APP% -c "set app.tenant_id = 't1'; select case when count(*) = 1 then 'OK   - t1 sees exactly its own 1 row' else 'FAIL - t1 expected 1 row, got ' || count(*) end from demo_doc;" > "%OUT%" 2>&1
type "%OUT%"
findstr /c:"FAIL" "%OUT%" >nul && set /a FAIL+=1

%APP% -c "set app.tenant_id = 't2'; select case when count(*) = 1 then 'OK   - t2 sees exactly its own 1 row' else 'FAIL - t2 expected 1 row, got ' || count(*) end from demo_doc;" > "%OUT%" 2>&1
type "%OUT%"
findstr /c:"FAIL" "%OUT%" >nul && set /a FAIL+=1

echo.
echo ============================================================
echo  5) Missing tenant context must yield ZERO rows
echo ============================================================
%APP% -c "select case when count(*) = 0 then 'OK   - no context, no rows (invariant 2 holds)' else 'FAIL - leaked ' || count(*) || ' rows without tenant context' end from demo_doc;" > "%OUT%" 2>&1
type "%OUT%"
findstr /c:"FAIL" "%OUT%" >nul && set /a FAIL+=1

echo.
echo ============================================================
echo  6) Cross-tenant write must be rejected
echo ============================================================
%APP% -c "set app.tenant_id = 't1'; insert into demo_doc values ('sneak', 't2', 'written by t1');" > "%OUT%" 2>&1
if errorlevel 1 (
    echo OK   - rejected, as it should be
) else (
    echo FAIL - t1 was allowed to write a t2 row
    set /a FAIL+=1
)

echo.
echo ============================================================
if "%FAIL%"=="0" (
    echo  RESULT: ALL CHECKS PASSED
) else (
    echo  RESULT: %FAIL% CHECK^(S^) FAILED
)
echo ============================================================

del "%OUT%" >nul 2>&1
if not "%FAIL%"=="0" exit /b 1
endlocal

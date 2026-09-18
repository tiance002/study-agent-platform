@echo off
setlocal
REM ============================================================
REM  Start the control plane (in-process development adapters)
REM
REM  Interpreter resolution order:
REM    1. %STUDY_PLATFORM_PYTHON%       explicit override (e.g. a conda env)
REM    2. .venv\Scripts\python.exe      project virtualenv
REM    3. python.exe found on PATH
REM
REM  Why there is a dependency check below:
REM    A half-installed virtualenv still contains python.exe but no
REM    packages. The naive "does python.exe exist?" check passes, the
REM    server starts, and then dies with a confusing ModuleNotFoundError
REM    from inside uvicorn. Fail loudly HERE instead, where the message
REM    can say what to do about it.
REM
REM  Pure ASCII on purpose - see the note in pg_start.cmd.
REM ============================================================

cd /d "%~dp0.."

set "PY="
if not "%STUDY_PLATFORM_PYTHON%"=="" set "PY=%STUDY_PLATFORM_PYTHON%"
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY for %%I in (python.exe) do set "PY=%%~$PATH:I"

if not defined PY (
    echo [ERROR] No Python interpreter found.
    echo.
    echo   Create one with:
    echo     python -m venv .venv
    echo     .venv\Scripts\python -m pip install -e ".[dev]"
    echo.
    echo   Or point this script at an existing interpreter:
    echo     set STUDY_PLATFORM_PYTHON=C:\path\to\python.exe
    echo.
    exit /b 1
)

echo   Interpreter: %PY%

REM ---- Dependency gate ----------------------------------------
"%PY%" -c "import fastapi, uvicorn, pydantic, yaml" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] That interpreter is missing project dependencies.
    echo.
    echo   A virtualenv that has python.exe but no packages is a common
    echo   broken state after an interrupted pip install. Do NOT try to
    echo   repair it in place - recreate it:
    echo.
    echo     rmdir /s /q .venv
    echo     python -m venv .venv
    echo     .venv\Scripts\python -m pip install -e ".[dev]"
    echo.
    echo   If the editable install keeps failing, install the deps
    echo   directly - this app loads its own code via PYTHONPATH, so
    echo   installing the project itself is not required:
    echo.
    echo     .venv\Scripts\python -m pip install fastapi uvicorn pydantic PyYAML pytest httpx ruff mypy
    echo.
    exit /b 1
)

REM ---- Session signing secret ---------------------------------
REM Production must inject this from KMS / Secret Manager.
if "%STUDY_PLATFORM_SESSION_SECRET%"=="" set "STUDY_PLATFORM_SESSION_SECRET=dev-only-session-secret-change-me"

set "PYTHONPATH=%CD%\backend"

echo ============================================================
echo   Control plane: http://127.0.0.1:8000/
echo ============================================================
echo   Open the page, then paste a session token. Issue one with:
echo     %PY% tools\issue_session.py --tenant tenant_demo --principal user_demo
echo.
echo   Demo data available out of the box:
echo     tenant tenant_demo / principal user_demo / project proj_demo
echo.
echo   Press Ctrl+C to stop.
echo ============================================================
echo.

"%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
endlocal

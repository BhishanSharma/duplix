@echo off
REM Flight Allocation — one-click launcher.
REM First run: auto-creates .venv + installs deps (~1 min).
REM Subsequent runs: just activates the venv and starts the web UI.
REM Once it opens: today's flight schedule goes on the dashboard, the
REM two roster files go in the Setup sidebar (set once per period).

setlocal enabledelayedexpansion
set "REPO_ROOT=%~dp0"
cd /d "%REPO_ROOT%"

REM ---- Bootstrap .venv on first run ------------------------------------
if not exist "%REPO_ROOT%.venv\Scripts\activate.bat" (
    echo.
    echo No .venv\ found — bootstrapping. This is a one-time ~2 min setup.
    echo.

    REM Find Python 3.11-3.14 (what the pinned packages support). Try the
    REM py launcher's versions newest first, then plain "python".
    set "PY_CMD="
    set "PY_OK=import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 14) else 1)"
    where py >nul 2>nul
    if !errorlevel! equ 0 (
        for %%v in (3.14 3.13 3.12 3.11) do (
            if not defined PY_CMD (
                py -%%v -c "!PY_OK!" >nul 2>nul
                if !errorlevel! equ 0 set "PY_CMD=py -%%v"
            )
        )
    )
    if not defined PY_CMD (
        where python >nul 2>nul
        if !errorlevel! equ 0 (
            python -c "!PY_OK!" >nul 2>nul
            if !errorlevel! equ 0 set "PY_CMD=python"
        )
    )
    if not defined PY_CMD (
        echo.
        echo ERROR: No supported Python found ^(needs 3.11 to 3.14^).
        for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo   Found: %%v
        echo   1. Install Python 3.13 from https://python.org
        echo   2. During install, tick "Add Python to PATH"
        echo   3. Double-click this file again.
        echo.
        pause
        exit /b 1
    )
    echo Using !PY_CMD! ^(Python 3.11-3.14 required^).

    echo Creating .venv with !PY_CMD! ...
    !PY_CMD! -m venv .venv
    if errorlevel 1 (
        echo.
        echo ERROR: venv creation failed.
        pause
        exit /b 1
    )

    call "%REPO_ROOT%.venv\Scripts\activate.bat"
    if errorlevel 1 (
        echo.
        echo ERROR: venv activate failed.
        pause
        exit /b 1
    )

    echo Installing dependencies ^(openpyxl, ortools, pydantic, pyyaml^) ...
    python -m pip install --upgrade pip >nul 2>nul
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo ERROR: pip install -r requirements.txt failed.
        echo Check the messages above. Try running manually:
        echo   .venv\Scripts\activate
        echo   pip install -r requirements.txt
        pause
        exit /b 1
    )

    echo.
    echo Setup complete. Launching the web UI ...
    echo.
    goto run
)

call "%REPO_ROOT%.venv\Scripts\activate.bat"
if errorlevel 1 (
    echo.
    echo ERROR: Failed to activate .venv at %REPO_ROOT%.venv
    echo If the venv is corrupted, delete the .venv folder and re-run this file.
    pause
    exit /b 1
)

:run
set "PYTHONPATH=%REPO_ROOT%;%PYTHONPATH%"
python -m src.web
set "EXIT_CODE=!errorlevel!"
if !EXIT_CODE! neq 0 (
    echo.
    echo ============================================================
    echo The Python launcher exited with error code !EXIT_CODE!.
    echo Scroll up to see the actual error message.
    echo ============================================================
    echo.
    pause
)
endlocal
REM Always pause on exit so the window stays open and any error is
REM visible. Comment out the next line if you launch from a terminal
REM that already keeps the window after exit.
echo.
echo (Press any key to close this window.)
pause >nul

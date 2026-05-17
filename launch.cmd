@echo off
REM FFXIV Completion Tracker -- local launcher.
REM Bootstraps a Python virtualenv + dependencies on first run, then hands
REM off to launch.py which renders the interactive text menu.

setlocal EnableDelayedExpansion

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "VENV_DIR=%ROOT%\.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "REQUIREMENTS=%ROOT%\requirements.txt"

REM ---------------------------------------------------------------------------
REM First-run: locate Python and create .venv if missing.
REM ---------------------------------------------------------------------------
if not exist "%VENV_PY%" (
    echo [setup] No virtualenv found at .venv\
    set "BOOT_PY="

    REM Prefer the py launcher (handles multiple installs cleanly).
    where py >nul 2>&1
    if not errorlevel 1 (
        for /f "delims=" %%V in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "BOOT_PY=%%V"
    )
    if not defined BOOT_PY (
        where python >nul 2>&1
        if not errorlevel 1 (
            for /f "delims=" %%V in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "BOOT_PY=%%V"
        )
    )
    if not defined BOOT_PY (
        echo [setup] Python 3.10+ not found on PATH.
        echo         Install it from https://www.python.org/ and re-run launch.cmd.
        pause
        exit /b 1
    )

    for /f "delims=" %%V in ('"!BOOT_PY!" --version 2^>^&1') do echo [setup] Found %%V at !BOOT_PY!

    "!BOOT_PY!" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
    if errorlevel 1 (
        echo [setup] Python 3.10 or newer is required.
        pause
        exit /b 1
    )

    echo [setup] Creating virtualenv in %VENV_DIR%
    "!BOOT_PY!" -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [setup] Virtualenv creation failed.
        pause
        exit /b 1
    )
)

REM ---------------------------------------------------------------------------
REM Dependency check: only install when fastapi is missing (sentinel import).
REM ---------------------------------------------------------------------------
"%VENV_PY%" -c "import fastapi" >nul 2>&1
if errorlevel 1 (
    echo [setup] Installing dependencies from requirements.txt
    "%VENV_PY%" -m pip install --upgrade pip
    if errorlevel 1 (
        echo [setup] pip upgrade failed.
        pause
        exit /b 1
    )
    "%VENV_PY%" -m pip install -r "%REQUIREMENTS%"
    if errorlevel 1 (
        echo [setup] Dependency install failed.
        pause
        exit /b 1
    )
)

REM ---------------------------------------------------------------------------
REM Hand off to the interactive menu.
REM ---------------------------------------------------------------------------
"%VENV_PY%" "%ROOT%\launch.py" %*
exit /b %ERRORLEVEL%

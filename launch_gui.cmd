@echo off
REM FFXIV Completion Tracker -- GUI launcher (no console window).
REM Used by the installer's desktop / Start Menu shortcuts.

setlocal EnableDelayedExpansion

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

REM ---------------------------------------------------------------------------
REM Bundled-Python path (Inno Setup installer): no venv, no bootstrap.
REM Use pythonw.exe so the GUI runs without a console window.
REM ---------------------------------------------------------------------------
if exist "%ROOT%\python\pythonw.exe" (
    start "" "%ROOT%\python\pythonw.exe" "%ROOT%\launch_gui.py" %*
    exit /b 0
)
if exist "%ROOT%\python\python.exe" (
    start "" "%ROOT%\python\python.exe" "%ROOT%\launch_gui.py" %*
    exit /b 0
)

REM ---------------------------------------------------------------------------
REM Source-checkout path: defer to launch.cmd to make sure the venv exists
REM and deps are installed, then hand off to the GUI via pythonw.
REM ---------------------------------------------------------------------------
if not exist "%ROOT%\.venv\Scripts\pythonw.exe" (
    REM First run: bootstrap via the existing CLI launcher (it knows how).
    call "%ROOT%\launch.cmd" --bootstrap-only
)

if exist "%ROOT%\.venv\Scripts\pythonw.exe" (
    start "" "%ROOT%\.venv\Scripts\pythonw.exe" "%ROOT%\launch_gui.py" %*
    exit /b 0
)

echo Could not locate a Python runtime to launch the GUI.
pause
exit /b 1

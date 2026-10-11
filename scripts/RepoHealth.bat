@echo off
REM Double-click launcher for a source checkout on Windows.
REM
REM Put this file anywhere, or right-click it and "Send to > Desktop
REM (create shortcut)" to start RepoHealth without opening a terminal.
REM
REM It activates the project's virtual environment first, so it works whether
REM or not the user has one active.

setlocal
cd /d "%~dp0\.."

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else (
    echo No virtual environment found at .venv
    echo Run this first:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -e ".[dashboard]"
    pause
    exit /b 1
)

python -m repohealth app --window

REM Keep the window open if something went wrong, so the error is readable.
if errorlevel 1 pause
endlocal

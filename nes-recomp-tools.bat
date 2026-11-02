@echo off
rem Launch NESRecomp Tools on Windows. On first run, create the
rem bundled virtual environment and install the app's Python deps.
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
    echo First run: setting up the Python environment...
    py -3 -m venv .venv || python -m venv .venv
    if errorlevel 1 (
        echo Could not create the environment. Install Python 3.11+
        echo from python.org, then run this file again.
        pause
        exit /b 1
    )
    .venv\Scripts\pip install -r requirements.txt
    if errorlevel 1 (
        echo Dependency install failed - check your connection and re-run.
        pause
        exit /b 1
    )
)
.venv\Scripts\python app\main.py %*

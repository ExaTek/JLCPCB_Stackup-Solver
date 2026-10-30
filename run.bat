@echo off
rem Start from source on Windows: the first run makes .venv and installs the requirements.
cd /d "%~dp0"
if not exist .venv\Scripts\pythonw.exe (
    py -3 -m venv .venv 2>nul || python -m venv .venv || (echo Python 3.10+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
    .venv\Scripts\python -m pip install --upgrade pip
    .venv\Scripts\python -m pip install -r requirements.txt || (pause & exit /b 1)
)
start "" .venv\Scripts\pythonw.exe -m jlc %*

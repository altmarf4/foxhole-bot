@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    py -3.13 -m venv .venv || py -3.12 -m venv .venv || py -3.11 -m venv .venv || python -m venv .venv
    .venv\Scripts\python.exe -m pip install --upgrade pip
    .venv\Scripts\python.exe -m pip install -r requirements.txt
)
.venv\Scripts\python.exe -m foxbot
pause

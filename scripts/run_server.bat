@echo off
REM Start the dashboard + API at http://localhost:8000
cd /d "%~dp0.."
.venv\Scripts\python.exe -m uvicorn app.server:app --host 127.0.0.1 --port 8000

@echo off
REM One-time setup on a fresh machine: Python venv + deps + frontend build.
REM Prereqs: Python 3.11+ and Node 18+ on PATH.
cd /d "%~dp0.."
echo === Creating Python venv and installing backend deps ===
py -m venv .venv
call .venv\Scripts\python.exe -m pip install --upgrade pip
call .venv\Scripts\python.exe -m pip install -r requirements.txt
echo === Installing and building the frontend ===
call npm install --prefix frontend
call npm run build --prefix frontend
echo.
echo Setup complete. Next:
echo   scripts\seed_demo.bat     (synthetic data to explore the UI), then
echo   scripts\run_server.bat    and open http://localhost:8000
echo For live data on an unfiltered network: scripts\backfill.bat then scripts\run_collector.bat

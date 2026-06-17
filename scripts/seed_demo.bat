@echo off
REM Seed synthetic demo data so the dashboard works with NO network access.
REM Pass-through args, e.g.: scripts\seed_demo.bat --days 90
cd /d "%~dp0.."
.venv\Scripts\python.exe -m app.mockdata %*

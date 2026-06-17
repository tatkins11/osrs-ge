@echo off
REM Live 5-minute price collector. Leave this running on an UNFILTERED network.
REM (On this corporate network the OSRS API is blocked by FortiGuard.)
cd /d "%~dp0.."
.venv\Scripts\python.exe -m app.collector

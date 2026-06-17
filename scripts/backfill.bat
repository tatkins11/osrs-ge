@echo off
REM Seed historical price data from the wiki /timeseries API (run on an unfiltered network).
REM Pass-through args, e.g.: scripts\backfill.bat --timestep 6h
cd /d "%~dp0.."
.venv\Scripts\python.exe -m app.backfill %*

@echo off
rem Starts the background worker and the local UI in separate windows (closing the UI does not stop the worker).
cd /d "%~dp0.."
set PYTHONUTF8=1
start "HR Sorter worker" /min .venv\Scripts\python.exe -m hrsorter.cli worker
start "HR Sorter UI" /min .venv\Scripts\python.exe -m hrsorter.cli ui
timeout /t 3 >nul
start "" http://127.0.0.1:8765/

@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m proctoring --demo
) else (
  python -m proctoring --demo
)
if errorlevel 1 pause

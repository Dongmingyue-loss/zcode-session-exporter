@echo off
setlocal
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
where python >nul 2>nul
if %errorlevel%==0 (
  python app.py %*
) else (
  py app.py %*
)

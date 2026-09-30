@echo off
chcp 65001 >nul
cd /d "%~dp0"
title SyncVoice
if not exist venv (
  echo Сначала запустите setup.bat
  pause
  exit /b 1
)
venv\Scripts\python manage.py migrate --noinput >nul
venv\Scripts\python manage.py run_app
pause

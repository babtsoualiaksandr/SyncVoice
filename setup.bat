@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === Установка SyncVoice ===

where py >nul 2>nul
if errorlevel 1 (
  echo Не найден Python. Установите Python 3.12 с https://www.python.org/downloads/windows/
  echo ^(при установке отметьте "Add python.exe to PATH"^) и запустите setup.bat ещё раз.
  pause
  exit /b 1
)

if not exist venv (
  echo Создаю виртуальное окружение...
  py -3.12 -m venv venv || py -3 -m venv venv
  if errorlevel 1 ( echo Не удалось создать venv. & pause & exit /b 1 )
)

echo Устанавливаю зависимости (несколько минут)...
venv\Scripts\python -m pip install --upgrade pip
venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 ( echo Ошибка установки зависимостей. & pause & exit /b 1 )

echo Готовлю базу данных...
venv\Scripts\python manage.py migrate --noinput
if errorlevel 1 ( echo Ошибка подготовки базы данных. & pause & exit /b 1 )

echo Создаю ярлык на рабочем столе...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop') + '\SyncVoice.lnk');" ^
  "$s.TargetPath = '%~dp0SyncVoice.bat'; $s.WorkingDirectory = '%~dp0'; $s.Save()"

echo.
echo Готово. Запускайте SyncVoice ярлыком на рабочем столе.
echo При первом запуске скачается модель распознавания (~500 МБ).
pause

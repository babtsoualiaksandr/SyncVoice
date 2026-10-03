@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion
rem SyncVoice: install, update and (re)start in one script — the desktop shortcut runs it.
rem   1. stops a running SyncVoice   2. git pull   3. Python environment and packages (when changed)
rem   4. database migrations   5. desktop shortcut   6. starts SyncVoice
rem Options: --no-pull — start without updating (no internet).

rem «git pull» may rewrite this very file while cmd is reading it: run a copy from %TEMP%.
if /i not "%~1"=="--copy" (
  copy /y "%~f0" "%TEMP%\SyncVoice-run.bat" >nul
  "%TEMP%\SyncVoice-run.bat" --copy "%~dp0" %*
)
set "ROOT=%~2"
set "OPTION=%~3"
cd /d "%ROOT%"
title SyncVoice — обновление и запуск
echo === SyncVoice: обновление и запуск ===
echo.

rem ---------- 1. Stop a running SyncVoice (its python and the console window it runs in) ----------
echo [1/6] Останавливаю запущенный SyncVoice...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$apps = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*manage.py*run_app*' });" ^
  "foreach ($p in $apps) {" ^
  "  $parent = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $p.ParentProcessId) -ErrorAction SilentlyContinue;" ^
  "  Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue;" ^
  "  if ($parent -and $parent.Name -eq 'cmd.exe' -and $parent.CommandLine -like '*SyncVoice*') { Stop-Process -Id $parent.ProcessId -Force -ErrorAction SilentlyContinue }" ^
  "};" ^
  "if ($apps.Count) { '      остановлен' } else { '      не был запущен' }"
timeout /t 2 /nobreak >nul

rem ---------- 2. Update from git ----------
if /i "%OPTION%"=="--no-pull" (
  echo [2/6] Обновление пропущено: --no-pull
  goto environment
)
echo [2/6] Скачиваю обновления...
where git >nul 2>nul
if errorlevel 1 (
  echo       Git не найден — обновление пропущено. Установите Git for Windows, чтобы обновляться.
  goto environment
)
set "OLD="
set "NEW="
for /f %%h in ('git rev-parse HEAD 2^>nul') do set "OLD=%%h"
git pull --ff-only
if errorlevel 1 (
  echo.
  echo  [!] Обновить не удалось: нет интернета или в папке изменены файлы. Запускаю текущую версию.
  echo      Посмотреть изменённые файлы: git status
  echo.
  goto environment
)
for /f %%h in ('git rev-parse HEAD') do set "NEW=%%h"
if "%OLD%"=="%NEW%" (
  echo       Обновлений нет.
) else (
  echo       Новое в этой версии:
  git log --oneline --no-decorate %OLD%..%NEW%
)

:environment
rem ---------- 3. Python environment and packages ----------
echo [3/6] Проверяю окружение Python...
if exist venv\Scripts\python.exe goto packages
where py >nul 2>nul
if errorlevel 1 (
  echo  [!] Не найден Python. Установите Python 3.12 с https://www.python.org/downloads/windows/
  echo      При установке отметьте «Add python.exe to PATH» и запустите SyncVoice.bat ещё раз.
  goto failed
)
echo       Создаю виртуальное окружение...
py -3.12 -m venv venv || py -3 -m venv venv
if not exist venv\Scripts\python.exe (
  echo  [!] Не удалось создать окружение Python.
  goto failed
)
venv\Scripts\python -m pip install --upgrade pip

:packages
rem Reinstall packages only when requirements.txt changed since the last successful install.
set "WANTED="
set "INSTALLED="
for /f %%h in ('powershell -NoProfile -Command "(Get-FileHash requirements.txt).Hash"') do set "WANTED=%%h"
if exist venv\requirements.sha set /p INSTALLED=<venv\requirements.sha
if not defined WANTED goto install
if "%WANTED%"=="%INSTALLED%" (
  echo       Пакеты в порядке.
  goto migrate
)
:install
echo       Устанавливаю пакеты, это может занять несколько минут...
venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 (
  echo  [!] Ошибка установки пакетов.
  goto failed
)
if defined WANTED > venv\requirements.sha echo %WANTED%

:migrate
rem ---------- 4. Database ----------
echo [4/6] Обновляю базу данных...
venv\Scripts\python manage.py migrate --noinput
if errorlevel 1 (
  echo  [!] Ошибка обновления базы. Если «database is locked» — закройте окна с командами manage.py и запустите ещё раз.
  goto failed
)

rem ---------- 5. Desktop shortcut ----------
echo [5/6] Ярлык на рабочем столе...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$path = [Environment]::GetFolderPath('Desktop') + '\SyncVoice.lnk';" ^
  "if (Test-Path $path) { '      есть' } else {" ^
  "  $s = (New-Object -ComObject WScript.Shell).CreateShortcut($path);" ^
  "  $s.TargetPath = '%ROOT%SyncVoice.bat'; $s.WorkingDirectory = '%ROOT%'; $s.Save(); '      создан' }"
if not exist .env (
  echo       Файла .env нет — подсказки ИИ выключены. Образец: .env.example
)

rem ---------- 6. Start ----------
echo [6/6] Запускаю SyncVoice. При первом запуске скачается модель распознавания, около 500 МБ.
echo.
title SyncVoice
venv\Scripts\python manage.py run_app
echo.
echo SyncVoice остановлен.
pause
exit /b 0

:failed
echo.
pause
exit /b 1

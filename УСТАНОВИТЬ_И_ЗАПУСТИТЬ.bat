@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Научный клубок — установка и запуск
echo ============================================
echo   Научный клубок — установка и запуск
echo ============================================

set PY=
py --version >nul 2>&1 && set PY=py
if "%PY%"=="" (
  python --version >nul 2>&1 && set PY=python
)

if "%PY%"=="" (
  echo [1/4] Python не найден. Скачиваю установщик...
  curl -L -o "%TEMP%\python-installer.exe" https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe
  if errorlevel 1 (
    echo ОШИБКА: не удалось скачать Python. Установите вручную с python.org
    pause
    exit /b 1
  )
  echo [2/4] Устанавливаю Python — это займёт 2-3 минуты...
  "%TEMP%\python-installer.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_test=0
  set "PATH=%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
  set PY=python
  %PY% --version >nul 2>&1
  if errorlevel 1 (
    echo ОШИБКА: Python установлен, но не найден. Закройте окно и запустите этот файл ещё раз.
    pause
    exit /b 1
  )
) else (
  echo [1/4] Python найден: & %PY% --version
)

echo [3/4] Устанавливаю зависимости...
%PY% -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo ОШИБКА при установке зависимостей. Текст ошибки выше.
  pause
  exit /b 1
)

echo [4/4] Запускаю сервер: http://localhost:8000
echo Не закрывайте это окно, пока работаете с системой. Остановка: Ctrl+C
start "" http://localhost:8000
%PY% -m uvicorn app.main:app --port 8000
pause

@echo off
chcp 65001 > nul
cd /d "%~dp0"
title MultiTTSBOT - %~dp0
rem /restart で終了したとき（終了コード3）は、すぐに起動し直す。それ以外で終了したら10秒待って起動し直す
set MULTITTSBOT_LOOP=1

set "PY="
if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if not defined PY where py > nul 2>&1 && set "PY=py"
if not defined PY set "PY=python"

:loop
echo [%date% %time%] BOTを起動します
"%PY%" MultiTTSBOT.py
set CODE=%errorlevel%
if "%CODE%"=="3" goto loop
echo.
echo [%date% %time%] BOTが終了しました（終了コード %CODE%）。10秒後に起動し直します。
echo 止めるときは、このウィンドウを閉じてください。
timeout /t 10 > nul
goto loop

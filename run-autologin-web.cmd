@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "MODE=%~1"
set "PY="
set "PYA="

call py -3 -V >nul 2>&1
if not errorlevel 1 (
  set "PY=py"
  set "PYA=-3"
  goto :found
)
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
  if exist "%%D\python.exe" (
    set "PY=%%D\python.exe"
    set "PYA="
    goto :found
  )
)
for /f "delims=" %%P in ('where python 2^>nul') do (
  set "PY=%%P"
  set "PYA="
  goto :found
)
:found
if "%PY%"=="" goto :nopython

chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

if /i "%MODE%"=="hidden" goto :hidden
if not "%MODE%"=="" goto :args

rem ---------------- first launch by double click ----------------
title CampusAutoLoginWeb
echo.
echo   CampusAutoLoginWeb  -  auto login watchdog
echo   ------------------------------------------------------------
echo   python : %PY%
echo   config : autologin-web.json
echo   log    : autologin-web.log
echo   limit  : 00:00-06:30 no action (see skip_start / skip_end)
echo.
call :ensure_autostart
call :running
if not errorlevel 1 goto :already

echo   [+] starting in background, no window will stay open
ping -n 2 127.0.0.1 >nul
start "" wscript.exe "%~dp0start-hidden.vbs"
ping -n 4 127.0.0.1 >nul
call :running
if errorlevel 1 goto :failed
echo   [ok] running in background - this window closes now
ping -n 3 127.0.0.1 >nul
exit /b 0

:already
echo   [i] already running - this window closes now
ping -n 3 127.0.0.1 >nul
exit /b 0

:failed
echo   [!] background start failed - see autologin-web.log
echo       try: start-hidden.vbs  (double click)
ping -n 6 127.0.0.1 >nul
exit /b 1

rem ---------------- started by start-hidden.vbs (no window) ----------------
:hidden
call :ensure_autostart_quiet
goto :loop

rem ---------------- with arguments: --diagnose / --once / --force ----------------
:args
call "%PY%" %PYA% autologin-web.py %*
exit /b %errorlevel%

rem ---------------- supervisor loop ----------------
:loop
call "%PY%" %PYA% autologin-web.py
set "RC=%errorlevel%"
if "%RC%"=="0" goto :loop_quiet
if "%RC%"=="2" exit /b 2
if "%RC%"=="3" exit /b 3
echo [%date% %time%] exited code=%RC%, restart in 15s>> autologin-web-supervisor.log
:loop_quiet
ping -n 16 127.0.0.1 >nul
goto :loop

rem ---------------- helpers ----------------
:ensure_autostart
call :write_autostart
if exist "%STARTUP%" (
  if "%HAD%"=="1" (
    echo   [i] autostart refreshed: %STARTUP%
  ) else (
    echo   [+] autostart registered: %STARTUP%
  )
) else (
  echo   [!] autostart not written, run install-autologin-web.cmd
)
exit /b 0

:ensure_autostart_quiet
call :write_autostart >nul 2>&1
exit /b 0

:write_autostart
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\CampusAutoLoginWeb.cmd"
if not exist "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup" exit /b 0
set "HAD=0"
if exist "%STARTUP%" set "HAD=1"
> "%STARTUP%" echo @echo off
>> "%STARTUP%" echo wscript.exe "%~dp0start-hidden.vbs"
exit /b 0

:running
set "LK="
if exist "%~dp0autologin-web.lock" set /p LK=<"%~dp0autologin-web.lock"
if "%LK%"=="" exit /b 1
set "LN="
for /f "tokens=1 delims=," %%A in ('tasklist /FI "PID eq %LK%" /FO CSV /NH 2^>nul') do set "LN=%%~A"
if /i "%LN%"=="python.exe" exit /b 0
if /i "%LN%"=="pythonw.exe" exit /b 0
exit /b 1

:nopython
echo   [x] python not found - install Python 3 or edit this file
if /i "%MODE%"=="hidden" exit /b 2
ping -n 6 127.0.0.1 >nul
exit /b 2

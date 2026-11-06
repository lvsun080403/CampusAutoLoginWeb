@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "HERE=%~dp0"
set "RUNNER=!HERE!run-autologin-web.cmd"
set "HIDER=!HERE!start-hidden.vbs"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LNKCMD=!STARTUP!\CampusAutoLoginWeb.cmd"

if not exist "!RUNNER!" goto :norunner
if not exist "!HIDER!" goto :nohider

echo [1/4] register autostart (windowless)
if not exist "!STARTUP!" mkdir "!STARTUP!"
> "!LNKCMD!" echo @echo off
>> "!LNKCMD!" echo wscript.exe "!HIDER!"
if not exist "!LNKCMD!" goto :fail
echo     !LNKCMD!

echo [2/4] optional scheduled task (needs admin)
schtasks /create /tn "CampusAutoLoginWeb" /sc onlogon /tr "wscript.exe \"!HIDER!\"" /f >nul 2>&1
if errorlevel 1 goto :notadmin
echo     CampusAutoLoginWeb registered
goto :step3
:notadmin
echo     skipped - not running as administrator
:step3

echo [3/4] start now (no window)
start "" wscript.exe "!HIDER!"
ping -n 5 127.0.0.1 >nul

echo [4/4] verify
set "LK="
if exist "autologin-web.lock" set /p LK=<"autologin-web.lock"
if "!LK!"=="" goto :nolock
set "LN="
for /f "tokens=1 delims=," %%A in ('tasklist /FI "PID eq !LK!" /FO CSV /NH 2^>nul') do set "LN=%%~A"
if /i "!LN!"=="python.exe" goto :ok
if /i "!LN!"=="pythonw.exe" goto :ok
goto :nolock
:ok
echo     running in background, python pid !LK!
goto :tail
:nolock
echo     no lock file yet - check autologin-web.log
:tail

echo.
echo done.
echo   config: autologin-web.json
echo   log   : autologin-web.log
echo   stop  : uninstall-autologin-web.cmd
ping -n 4 127.0.0.1 >nul
exit /b 0

:norunner
echo [x] run-autologin-web.cmd not found next to this script
ping -n 5 127.0.0.1 >nul
exit /b 2

:nohider
echo [x] start-hidden.vbs not found next to this script
ping -n 5 127.0.0.1 >nul
exit /b 2

:fail
echo [x] could not write startup entry
ping -n 5 127.0.0.1 >nul
exit /b 1

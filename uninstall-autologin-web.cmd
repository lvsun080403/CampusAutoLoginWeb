@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LNKCMD=!STARTUP!\CampusAutoLoginWeb.cmd"

echo [1/5] remove startup entry
if not exist "!LNKCMD!" goto :nolnk
del /f /q "!LNKCMD!"
echo     deleted !LNKCMD!
goto :step2
:nolnk
echo     not present
:step2

echo [2/5] remove scheduled task
schtasks /delete /tn "CampusAutoLoginWeb" /f >nul 2>&1
if errorlevel 1 goto :notask
echo     task deleted
goto :step3
:notask
echo     task not present
:step3

echo [3/5] stop supervisor window
for /f "delims=" %%P in ('powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'cmd.exe' -and $_.CommandLine -match 'run-autologin-web' } | ForEach-Object { $_.ProcessId }" 2^>nul') do (
  taskkill /PID %%P /T /F >nul 2>&1
  echo     killed supervisor %%P
)

echo [4/5] stop python watchdog
for /f "delims=" %%P in ('powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'autologin-web.py' } | ForEach-Object { $_.ProcessId }" 2^>nul') do (
  taskkill /PID %%P /F >nul 2>&1
  echo     killed python %%P
)

echo [5/5] kill debug browser on port 9333
for /f "tokens=5" %%P in ('netstat -ano -p TCP ^| findstr ":9333" ^| findstr LISTENING') do (
  taskkill /PID %%P /F >nul 2>&1
  echo     killed browser %%P
)
if exist "autologin-web.lock" del /f /q "autologin-web.lock"
echo.
echo done. nothing will auto-start any more.
ping -n 4 127.0.0.1 >nul
exit /b 0

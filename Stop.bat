@echo off
REM Stop Douyin Pipeline services

echo.
echo ============================================================
echo   Stopping Douyin Pipeline...
echo ============================================================
echo.

taskkill /F /IM pythonw.exe 2>nul
if not errorlevel 1 echo  [OK] linkserver stopped
taskkill /F /IM cpolar.exe 2>nul
if not errorlevel 1 echo  [OK] cpolar stopped
REM Kill any python.exe holding port 7890
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":7890" ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F 2>nul
)

echo.
echo  All services stopped.
echo  Double-click Start.bat to restart.
echo.
pause

@echo off
chcp 65001 >nul
REM ============================================================
REM   Douyin Pipeline — 一键启动
REM ============================================================
cd /d F:\DouyinPipeline

echo.
echo   ╔══════════════════════════════════════════╗
echo   ║    抖音自动管线 — 启动中...              ║
echo   ╚══════════════════════════════════════════╝
echo.
echo   [1/3] 防止锁屏/熄屏睡眠（修改电源计划，不持久断电自恢复）...
powercfg /change standby-timeout-ac 0 >nul 2>nul
powercfg /change hibernate-timeout-ac 0 >nul 2>nul
if %errorlevel% equ 0 (echo    [OK] 系统已设为永不睡眠（AC 电源）) else (echo    [!] 修改电源计划失败，可尝试右键→以管理员身份运行)

echo.
echo   [2/3] 关闭旧进程...
taskkill /F /IM pythonw.exe >nul 2>nul
taskkill /F /IM cpolar.exe >nul 2>nul
timeout /t 2 >nul

echo   [3/3] 启动服务器（后台运行，cpolar 自动连接）...
start "" /B .venv\Scripts\pythonw.exe scripts\linkserver.py
timeout /t 5 >nul

echo.
echo   ───────────────────────────────────────────
echo   [OK] 启动成功！锁屏/熄屏不再断服务
echo.
echo   本机访问:     http://localhost:7890
echo   公网地址:     页面顶部自动显示（免费版重启会变）
echo   停止服务:     双击 Stop.bat
echo   ───────────────────────────────────────────
echo.
timeout /t 3 >nul

@echo off
chcp 65001 >nul
title 启动 Muse 批量注册服务
color 0B
echo ========================================================
echo        正在检查并启动 Muse 注册服务，请稍候...
echo ========================================================

set "BASE_DIR=%~dp0"
if "%BASE_DIR:~-1%"=="\" set "BASE_DIR=%BASE_DIR:~0,-1%"
for %%I in ("%BASE_DIR%\..") do set "PARENT_DIR=%%~fI"
set "ICLOUD_DIR=%PARENT_DIR%\icloud"

:: 1. 检查并启动 iCloud 邮件中继服务 (端口 4173)
netstat -ano | findstr :4173 | findstr LISTENING >nul
if %errorlevel% equ 0 (
    echo [OK] iCloud 邮箱服务已在运行 (端口: 4173)
) else (
    if exist "%ICLOUD_DIR%\src\server.mjs" (
        echo [..] 正在启动 iCloud 邮箱服务 (端口: 4173)...
        start "iCloud-Service" /min cmd /c "cd /d "%ICLOUD_DIR%" && node src/server.mjs"
        ping 127.0.0.1 -n 3 >nul
    ) else (
        echo [!] 未检测到 ..\icloud 目录，跳过 4173 服务启动
    )
)

:: 2. 检查并启动 Muse 注册 WebUI (端口 8766)
netstat -ano | findstr :8766 | findstr LISTENING >nul
if %errorlevel% equ 0 (
    echo [OK] Muse 注册 WebUI 已在运行 (端口: 8766)
) else (
    echo [..] 正在启动 Muse 注册 WebUI 服务 (端口: 8766)...
    start "Muse-WebUI" /min cmd /c "cd /d "%BASE_DIR%" && python start_webui.py --port 8766 --no-browser"
    ping 127.0.0.1 -n 4 >nul
)

:: 3. 打开浏览器
echo [..] 正在打开浏览器: http://127.0.0.1:8766
start http://127.0.0.1:8766

echo ========================================================
echo [OK] Muse 注册服务启动完成！
echo      1. Muse 注册控制台: http://127.0.0.1:8766
echo      2. iCloud 邮箱控制台: http://127.0.0.1:4173/tasks
echo ========================================================
echo 本窗口将在 5 秒后自动关闭，后台服务会持续保持运行...
ping 127.0.0.1 -n 6 >nul

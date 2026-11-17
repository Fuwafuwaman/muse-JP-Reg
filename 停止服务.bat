@echo off
chcp 65001 >nul
title 停止 Muse 批量注册服务
echo ========================================================
echo        正在停止 Muse 注册服务 (端口: 8766)...
echo ========================================================

for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8766 ^| findstr LISTENING') do (
    echo [..] 正在停止 Muse 注册 WebUI 服务 (PID: %%a)...
    taskkill /f /pid %%a >nul 2>&1
)

echo [OK] Muse 注册服务已停止 (端口 8766)
echo ========================================================
ping 127.0.0.1 -n 3 >nul

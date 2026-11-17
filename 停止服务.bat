@echo off
chcp 65001 >nul
title Muse 登録サービスを停止しています
echo ========================================================
echo        Muse 登録サービスを停止しています ^(ポート: 8766)...
echo ========================================================

set "FOUND="
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8766 ^| findstr LISTENING') do (
    set "FOUND=1"
    echo [..] Muse 登録 WebUI を停止しています ^(PID: %%a^)...
    taskkill /f /pid %%a >nul 2>&1
)

if defined FOUND (
    echo [OK] Muse 登録サービスを停止しました ^(ポート 8766^)
) else (
    echo [!] ポート 8766 で待ち受けているプロセスは見つかりませんでした。
    echo     すでに停止しています。
)

echo ========================================================
ping 127.0.0.1 -n 3 >nul

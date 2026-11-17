@echo off
chcp 65001 >nul
title Muse 登録サービスを起動
color 0B
echo ========================================================
echo        Muse 登録サービスを起動しています。
echo        iCloud リレーサービス ^(ポート 4173^) も同時に起動します...
echo ========================================================

set "BASE_DIR=%~dp0"
if "%BASE_DIR:~-1%"=="\" set "BASE_DIR=%BASE_DIR:~0,-1%"
for %%I in ("%BASE_DIR%\..") do set "PARENT_DIR=%%~fI"
set "ICLOUD_DIR=%PARENT_DIR%\icloud"

set "PYDIR=%LOCALAPPDATA%\Programs\Python\Python312"
if exist "%PYDIR%\python.exe" set "PATH=%PYDIR%;%PATH%"

:: 1. iCloud リレーサービス (ポート 4173) の起動確認
netstat -ano | findstr :4173 | findstr LISTENING >nul
if %errorlevel% equ 0 (
    echo [OK] iCloud リレーサービスはすでに起動しています ^(ポート: 4173^)
) else (
    if exist "%ICLOUD_DIR%\src\server.mjs" (
        echo [..] iCloud リレーサービスを起動しています ^(ポート: 4173^)...
        start "iCloud-Service" /min cmd /c "cd /d "%ICLOUD_DIR%" && node src/server.mjs"
        ping 127.0.0.1 -n 3 >nul
    ) else (
        echo [!] ..\icloud フォルダが見つかりません。4173 のリレーサービスはスキップします
    )
)

:: 2. Muse 登録 WebUI (ポート 8766) の起動確認
netstat -ano | findstr :8766 | findstr LISTENING >nul
if %errorlevel% equ 0 (
    echo [OK] Muse 登録 WebUI はすでに起動しています ^(ポート: 8766^)
) else (
    echo [..] Muse 登録 WebUI を起動しています ^(ポート: 8766^)...
    start "Muse-WebUI" /min cmd /c "cd /d "%BASE_DIR%" && python start_webui.py --port 8766 --no-browser"
    ping 127.0.0.1 -n 4 >nul
)

:: 3. ブラウザを開く
echo [..] ブラウザで開いています: http://127.0.0.1:8766
start http://127.0.0.1:8766

echo ========================================================
echo [OK] Muse 登録サービスの起動処理が完了しました
echo      1. Muse 登録サービス画面: http://127.0.0.1:8766
echo      2. iCloud タスク画面: http://127.0.0.1:4173/tasks
echo ========================================================
echo このウィンドウは 6 秒後に自動で閉じます。停止するときは 停止服务.bat を実行してください...
ping 127.0.0.1 -n 6 >nul

@echo off
setlocal
cd /d "%~dp0"

rem ============================================
rem  LlamaMonitor build (PyInstaller --onedir)
rem  Double-click to build:
rem  dist\LlamaMonitor\LlamaMonitor.exe
rem ============================================

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo Install Python 3.12+ and tick "Add python.exe to PATH".
    pause
    exit /b 1
)

echo [1/3] Installing runtime dependencies...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install requirements.txt
    pause
    exit /b 1
)

echo [2/3] Ensuring PyInstaller is installed...
python -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo [ERROR] Failed to install pyinstaller
        pause
        exit /b 1
    )
)

rem 确保应用图标存在（tools/generate_icon.py 生成，多尺寸 ICO）
if not exist "assets\LlamaMonitor.ico" (
    echo [INFO] assets\LlamaMonitor.ico not found; generating dev icon...
    python tools\generate_icon.py
    if errorlevel 1 (
        echo [ERROR] Icon generation failed
        pause
        exit /b 1
    )
)

echo [3/3] Building (onedir)...
rem --windowed: 无控制台黑窗（关键启动失败写 %LOCALAPPDATA%\LlamaMonitor\logs\monitor.log）
rem --icon: EXE 图标与托盘共用 assets\LlamaMonitor.ico
python -m PyInstaller --noconfirm --clean --name LlamaMonitor ^
    --windowed ^
    --icon "assets\LlamaMonitor.ico" ^
    --add-data "static;static" ^
    --add-data "assets;assets" ^
    --add-data "config.example.json;." ^
    --collect-all webview ^
    --collect-all pythonnet ^
    --collect-all clr_loader ^
    --collect-all pystray ^
    --collect-submodules bottle ^
    --collect-submodules uvicorn ^
    desktop.py
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed.
    pause
    exit /b 1
)

if exist "dist\LlamaMonitor\LlamaMonitor.exe" (
    echo.
    echo [OK] Build succeeded: %cd%\dist\LlamaMonitor\LlamaMonitor.exe
    echo.
    echo - Copy the whole dist\LlamaMonitor folder to another Windows 11 machine to distribute.
    echo - App data: SQLite, config.json and logs live in %%LOCALAPPDATA%%\LlamaMonitor,
    echo   OUTSIDE this folder, so upgrading the EXE never deletes history data.
) else (
    echo [ERROR] LlamaMonitor.exe not found in dist\LlamaMonitor
    pause
    exit /b 1
)
pause

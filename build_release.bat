@echo off
setlocal
cd /d "%~dp0"

rem ============================================
rem  LlamaMonitor Release Build (Phase 12)
rem  双击或 CMD 运行：完整 Release 构建
rem  （测试 -> PyInstaller -> Portable ZIP
rem    -> Inno Setup Installer -> SHA256SUMS）
rem  输出: release\
rem ============================================

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    pause
    exit /b 1
)

python scripts\build_release.py %*
exit /b %ERRORLEVEL%

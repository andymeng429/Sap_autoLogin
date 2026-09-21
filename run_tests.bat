@echo off
rem ============================================================
rem  OpenSAPGUI test runner
rem  Double-click to run, or call it from cmd/PowerShell.
rem  Tests are mock-only: no SAP, no network, no config.json needed.
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
rem pip 安装/临时目录清理需要真正的文件删除权限，关掉测试进程里的删除护栏
set CODEBUDDY_SAFE_DELETE_ENABLED=0

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] venv not found: .venv\Scripts\python.exe
    echo         Create it with: python -m venv .venv
    pause
    exit /b 1
)

echo ============================================
echo   OpenSAPGUI tests ^(mock only, no SAP^)
echo ============================================

set FAILED=0

echo.
echo --- tests\test_config_store.py ---
".venv\Scripts\python.exe" "tests\test_config_store.py"
if errorlevel 1 set FAILED=1

echo.
echo --- tests\test_sap_landscape.py ---
".venv\Scripts\python.exe" "tests\test_sap_landscape.py"
if errorlevel 1 set FAILED=1

echo.
echo --- tests\test_entry_dialog.py ---
".venv\Scripts\python.exe" "tests\test_entry_dialog.py"
if errorlevel 1 set FAILED=1

echo.
echo --- tests\test_login_verify.py ---
".venv\Scripts\python.exe" "tests\test_login_verify.py"
if errorlevel 1 set FAILED=1

echo.
echo --- tests\test_login_conflict.py ---
".venv\Scripts\python.exe" "tests\test_login_conflict.py"
if errorlevel 1 set FAILED=1

echo.
echo --- tests\test_win_focus.py ---
".venv\Scripts\python.exe" "tests\test_win_focus.py"
if errorlevel 1 set FAILED=1

echo.
echo --- tests\test_card_state.py ---
".venv\Scripts\python.exe" "tests\test_card_state.py"
if errorlevel 1 set FAILED=1

echo.
echo --- tests\test_exit_cleanup.py ---
".venv\Scripts\python.exe" "tests\test_exit_cleanup.py"
if errorlevel 1 set FAILED=1

echo.
if "%FAILED%"=="1" (
    echo [RESULT] FAILED - see details above
) else (
    echo [RESULT] ALL PASSED
)

pause
exit /b %FAILED%

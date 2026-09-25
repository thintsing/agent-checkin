@echo off
REM ============================================================
REM  每日签到 · 一键运行
REM  用法：
REM     run_checkin.bat                正常运行（签到 + 提醒）
REM     run_checkin.bat --login        打开专用浏览器，人工登录一次（登录态长期复用）
REM     run_checkin.bat --probe        只读探针（查登录态与真实签到状态，不签到）
REM     run_checkin.bat --dry-run      演练（走流程不签到）
REM     run_checkin.bat --status       看各站点状态与连续天数
REM     run_checkin.bat --install-task   注册每日计划任务（免手工转义引号）
REM     run_checkin.bat --uninstall-task 卸载计划任务
REM     run_checkin.bat --only-site workbuddy   只处理指定站点
REM  首次运行会自动创建本地 venv 并安装依赖。
REM ============================================================
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"

set "PY=%LOCALAPPDATA%\Programs\Python\Launcher\py.exe"
if not exist "%PY%" set "PY=py"

if not exist ".venv\Scripts\python.exe" (
  echo [初始化] 正在创建本地虚拟环境并安装依赖（仅首次，需要联网）...
  "%PY%" -m venv .venv || goto :fail
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto :fail
)

set "PYTHONPATH=src"
".venv\Scripts\python.exe" -m checkin %*
if errorlevel 1 goto :fail

echo.
echo 完成。
pause
exit /b 0

:fail
echo.
echo [错误] 运行失败，请查看上方日志。首次使用请先在弹出的专用 Chrome 里登录 codebuddy.cn。
pause
exit /b 1

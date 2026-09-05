@echo off
rem 违禁词检测 - 一键启动脚本
rem 双击运行：自动使用项目内 Python 运行环境启动服务并打开浏览器
chcp 65001 >nul
cd /d "%~dp0"
if exist "python\python.exe" (
  "python\python.exe" "app\main.py"
) else (
  ".venv\Scripts\python.exe" "app\main.py"
)
if errorlevel 1 (
  echo.
  echo 启动失败，请查看 data\app.log 或联系开发人员。
  pause
)

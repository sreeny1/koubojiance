@echo off
chcp 65001 >nul
rem 生成绿色免安装包（便携 Python 运行时 + 依赖 + 源码；模型首启自动下载）
rem 统一走 pack_full.ps1 单一实现，避免双份脚本逻辑漂移
set HERE=%~dp0
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%pack_full.ps1"
if errorlevel 1 (
    echo.
    echo 打包失败，请查看上方错误信息。
    pause
)

@echo off
chcp 65001 >nul
rem 生成绿色免安装全量包（包含完整 Python 运行时 + 依赖 + 模型 + 源码），解压即用
rem 对中文 Windows 环境：bat 必须 UTF-8 无 BOM，开头 chcp 65001 确保系统编码兼容

set HERE=%~dp0
set ROOT=%HERE%..
set OUT_DIR=%ROOT%\..\build\full

if exist "%OUT_DIR%" (
    echo [1/6] 清理旧输出...
    rmdir /s /q "%OUT_DIR%"
    if errorlevel 1 exit /b 1
)
mkdir "%OUT_DIR%"
echo [2/6] 创建目录结构...

xcopy "%ROOT%\app" "%OUT_DIR%\app\" /e /i /y >nul || exit /b 1
xcopy "%ROOT%\tests" "%OUT_DIR%\tests\" /e /i /y >nul || exit /b 1
xcopy "%ROOT%\tools" "%OUT_DIR%\tools\" /e /i /y >nul || exit /b 1
xcopy "%ROOT%\scripts" "%OUT_DIR%\scripts\" /e /i /y >nul || exit /b 1
xcopy "%ROOT%\data" "%OUT_DIR%\data\" /e /i /y >nul || exit /b 1
xcopy "%ROOT%\.venv" "%OUT_DIR%\python\" /e /i /y >nul || exit /b 1

copy "%ROOT%\README.md" "%OUT_DIR%\" >nul || exit /b 1
copy "%ROOT%\requirements.txt" "%OUT_DIR%\" >nul || exit /b 1
copy "%ROOT%\口播违禁词检测.exe" "%OUT_DIR%\" >nul || exit /b 1
copy "%ROOT%\启动.bat" "%OUT_DIR%\" >nul || exit /b 1
rem WebView2 运行时依赖（与 exe 同目录，启动器内嵌界面必需）
copy "%ROOT%\Microsoft.Web.WebView2.Core.dll" "%OUT_DIR%\" >nul || exit /b 1
copy "%ROOT%\Microsoft.Web.WebView2.WinForms.dll" "%OUT_DIR%\" >nul || exit /b 1
copy "%ROOT%\WebView2Loader.dll" "%OUT_DIR%\" >nul || exit /b 1
copy "%ROOT%\WebView2Loader_x86.dll" "%OUT_DIR%\" >nul || exit /b 1

echo [3/6] 清理冗余（减小体积：__pycache__、*.pyc）...
for /r "%OUT_DIR%" %%d in (__pycache__) do (
    if exist "%%d" rmdir /s /q "%%d" 2>nul
)
del /s /q "%OUT_DIR%\*.pyc" 2>nul
del /s /q "%OUT_DIR%\*.pyo" 2>nul
del /s /q "%OUT_DIR%\pip-cache\*.zip" 2>nul 2>&1

echo [4/6] 清理测试视频（可以在线下重新生成，不占包体积）...
del /q "%OUT_DIR%\data\app.db-wal" 2>nul
del /q "%OUT_DIR%\data\app.db-shm" 2>nul
del /q "%OUT_DIR%\data\*.log" 2>nul
rem 保留 app.db 种子词库
rmdir /s /q "%OUT_DIR%\tests\media\*.mp4" 2>nul
rmdir /s /q "%OUT_DIR%\tests\media\*.wav" 2>nul

echo [5/6] 统计...
call :count_files "%OUT_DIR%"
call :size_folder "%OUT_DIR%"
echo 完成：%OUT_DIR%
echo 文件数：%files%，大小：%size% MB
echo.
echo [6/6] 打包 zip ...
powershell -ExecutionPolicy Bypass -Command ^
$dest = \"%ROOT%\..\build\口播违禁词检测_全量绿色包.zip\"; ^
if (Test-Path $dest) { Remove-Item -Force $dest }; ^
Compress-Archive -Path \"%OUT_DIR%\\*\" -DestinationPath $dest -CompressionLevel Optimal; ^
Write-Host \"zip 输出: $dest\"; ^
$zipped = (Get-Item $dest).Length / 1MB; ^
Write-Host \"压缩后: $([math]::Round($zipped,1)) MB\"
goto :eof

:count_files
set files=0
for /r %%f in (%1\*) do set /a files+=1
goto :eof

:size_folder
set size=0
for /r %%f in (%1\*) do for /f "%%s in ('echo %%~zf') do set /a size+=%%~zs
set /a size=size / (1024*1024)
goto :eof

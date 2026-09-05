# Pack this project into a portable green-folder release.
#
# Model strategy: the zip does NOT include the whisper model. On first launch the
# app auto-downloads it from the fastest of several CN-friendly mirrors (ModelScope
# / hf-mirror), fully automatic with resumable download + manual-download guide.
#
# ffmpeg: bundled via imageio-ffmpeg (pip dependency, ~90MB). Kept for reliability;
# the 3GB model is the dominant size and is what we exclude from the package.
#
# Strategy: build a self-contained tree at <root>\build\full :
#   - python\  = portable interpreter (copied from current Python base prefix)
#                + .venv site-packages merged in (recipient needs NO Python)
#   - app/ docs/ tools/ scripts/ data/(runtime, NO models/pip-cache/webview2-data)
#   - launcher exe + WebView2 DLLs + 启动.bat
#
# NOTE: build this on a machine whose .venv contains the FULL dependency set
# (including nvidia-cublas-cu12 / nvidia-cudnn-cu12) if you want NVIDIA GPU support
# on recipients' machines.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\pack_full.ps1
param(
    [string]$Root = (Split-Path $PSScriptRoot -Parent),
    [string]$PyBase = "",   # empty = auto-detect from .venv python's sys.base_prefix
    [string]$Venv = ""      # empty = <root>\.venv
)

$ErrorActionPreference = "Stop"
$Out = Join-Path $Root "build\full"
$Zip = Join-Path $Root "build\口播违禁词检测_绿色免安装版.zip"

if (-not $Venv) { $Venv = Join-Path $Root ".venv" }
if (-not $PyBase) {
    $pyExe = Join-Path $Venv "Scripts\python.exe"
    if (-not (Test-Path $pyExe)) { throw "未找到 $pyExe，请先重建虚拟环境" }
    $PyBase = (& $pyExe -c "import sys; print(sys.base_prefix)").Trim()
}

function Copy-Tree([string]$src, [string]$dst, [string[]]$Xd = @("__pycache__", "pip-cache")) {
    if (Test-Path $src) {
        $xdArgs = @()
        foreach ($d in $Xd) { $xdArgs += "/XD"; $xdArgs += $d }
        $args = @($src, $dst, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP") + $xdArgs
        robocopy @args | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE): $src -> $dst" }
    }
}

Write-Host "[1/6] clean old output"
Remove-Item $Out -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $Zip -Force -ErrorAction SilentlyContinue
New-Item $Out -ItemType Directory -Force | Out-Null

Write-Host "[2/6] copy portable python runtime ..."
Copy-Tree $PyBase (Join-Path $Out "python")
Copy-Tree (Join-Path $Venv "Lib\site-packages") (Join-Path $Out "python\Lib\site-packages")

Write-Host "[3/6] copy project files ..."
Copy-Tree (Join-Path $Root "app") (Join-Path $Out "app")
Copy-Tree (Join-Path $Root "docs") (Join-Path $Out "docs")
Copy-Tree (Join-Path $Root "tools") (Join-Path $Out "tools")
robocopy (Join-Path $Root "tests") (Join-Path $Out "tests") /E /XD __pycache__ media /NFL /NDL /NJH /NJS /NP | Out-Null
# data: 排除 pip-cache / models / webview2-data（模型首启自动下载，不占包体积）
robocopy (Join-Path $Root "data") (Join-Path $Out "data") /E /XD __pycache__ pip-cache models webview2-data /NFL /NDL /NJH /NJS /NP | Out-Null
Copy-Item (Join-Path $Root "README.md") $Out -Force
Copy-Item (Join-Path $Root "requirements.txt") $Out -Force
Copy-Item (Join-Path $Root "口播违禁词检测.exe") $Out -Force
Copy-Item (Join-Path $Root "启动.bat") $Out -Force
foreach ($dll in @("Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll",
                   "WebView2Loader.dll", "WebView2Loader_x86.dll")) {
    $srcDll = Join-Path $Root $dll
    if (Test-Path $srcDll) { Copy-Item $srcDll $Out -Force }
}

Write-Host "[4/6] reset runtime data (fresh db on first run)"
Get-ChildItem (Join-Path $Out "data") -Filter "app.db*" -ErrorAction SilentlyContinue | Remove-Item -Force
Get-ChildItem (Join-Path $Out "data") -Filter "*.log*" -ErrorAction SilentlyContinue | Remove-Item -Force
Remove-Item (Join-Path $Out "data\settings.json") -Force -ErrorAction SilentlyContinue

Write-Host "[5/6] verify portable interpreter + dependencies"
$py = Join-Path $Out "python\python.exe"
if (-not (Test-Path $py)) { throw "portable python not found: $py" }
& $py -c "import faster_whisper, ctranslate2, fastapi, uvicorn, pypinyin, zhconv, openpyxl, imageio_ffmpeg, httpx, tkinter; print('deps OK')"
if ($LASTEXITCODE -ne 0) { throw "dependency check failed" }

Write-Host "[6/6] create zip ..."
$ErrorActionPreference = "Continue"
$tar = Get-Command tar.exe -ErrorAction SilentlyContinue
if ($tar) {
    Push-Location $Out
    tar -a -c -f $Zip .
    Pop-Location
} else {
    Compress-Archive -Path (Join-Path $Out '*') -DestinationPath $Zip -CompressionLevel Optimal
}
$ErrorActionPreference = "Stop"
if (-not (Test-Path $Zip)) { throw "zip creation failed" }
$mb = [math]::Round((Get-Item $Zip).Length / 1MB, 1)
$files = (Get-ChildItem $Out -Recurse -File).Count
Write-Host ""
Write-Host "PACK DONE (models excluded - auto-downloaded on first run)"
Write-Host "  folder : $Out"
Write-Host "  zip    : $Zip ($mb MB, $files files)"

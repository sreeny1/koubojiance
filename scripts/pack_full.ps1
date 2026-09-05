# Pack this project into a portable green-folder release.
# KEEP THIS FILE ASCII-ONLY: PowerShell 5.1 parses BOM-less .ps1 as GBK on
# Chinese Windows, so any non-ASCII literal would break parsing. Chinese-named
# files (the exe and 启动.bat) are located via wildcard, not literal strings.
#
# Model strategy: the zip does NOT include the whisper model. On first launch the
# app auto-downloads it from the fastest of several CN-friendly mirrors (ModelScope
# / hf-mirror), fully automatic with resumable download + manual-download guide.
#
# ffmpeg: bundled via imageio-ffmpeg (pip dependency, ~90MB). Kept for reliability.
#
# Strategy: build a self-contained tree at <root>\build\full :
#   - python\  = portable interpreter (copied from current Python base prefix)
#                + .venv site-packages merged in (recipient needs NO Python)
#   - app/ docs/ tools/ scripts/ data/(runtime, NO models/pip-cache/webview2-data)
#   - launcher exe + WebView2 DLLs + 启动.bat (via wildcard)
#
# NOTE: build on a machine whose .venv has the FULL dependency set (including
# nvidia-cublas-cu12 / nvidia-cudnn-cu12) to enable NVIDIA GPU on recipients.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\pack_full.ps1
param(
    [string]$Root = (Split-Path $PSScriptRoot -Parent),
    [string]$PyBase = "",   # empty = auto-detect from .venv python's sys.base_prefix
    [string]$Venv = ""      # empty = <root>\.venv
)

$ErrorActionPreference = "Stop"
$Out = Join-Path $Root "build\full"
# ASCII zip name (renamed to Chinese by the caller for display)
$Zip = Join-Path $Root "build\lianjinci-portable.zip"

if (-not $Venv) { $Venv = Join-Path $Root ".venv" }
if (-not $PyBase) {
    $pyExe = Join-Path $Venv "Scripts\python.exe"
    if (-not (Test-Path $pyExe)) { throw "venv python not found: $pyExe" }
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
# Only copy the interpreter + stdlib; exclude base global site-packages and Scripts
# (avoid leaking global packages / conflicts). Only the venv site-packages is shipped.
Copy-Tree $PyBase (Join-Path $Out "python") @("__pycache__", "pip-cache", "site-packages", "Scripts")
Copy-Tree (Join-Path $Venv "Lib\site-packages") (Join-Path $Out "python\Lib\site-packages")

Write-Host "[3/6] copy project files ..."
Copy-Tree (Join-Path $Root "app") (Join-Path $Out "app")
Copy-Tree (Join-Path $Root "docs") (Join-Path $Out "docs")
Copy-Tree (Join-Path $Root "tools") (Join-Path $Out "tools")
robocopy (Join-Path $Root "tests") (Join-Path $Out "tests") /E /XD __pycache__ media /NFL /NDL /NJH /NJS /NP | Out-Null
# data: exclude pip-cache / models / webview2-data (model auto-downloaded on first run)
robocopy (Join-Path $Root "data") (Join-Path $Out "data") /E /XD __pycache__ pip-cache models webview2-data /NFL /NDL /NJH /NJS /NP | Out-Null
Copy-Item (Join-Path $Root "README.md") $Out -Force
Copy-Item (Join-Path $Root "requirements.txt") $Out -Force
# Chinese-named launcher exe and 启动.bat, located via wildcard (ASCII-safe)
$exe = Get-ChildItem $Root -Filter "*.exe" | Where-Object { -not $_.Name.Contains("WebView2") } | Select-Object -First 1
if ($exe) { Copy-Item $exe.FullName $Out -Force }
$bat = Get-ChildItem $Root -Filter "*.bat" | Select-Object -First 1
if ($bat) { Copy-Item $bat.FullName $Out -Force }
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
# Disable user site-packages so the check (and the shipped app) never picks up
# a conflicting package from the local user's Python directory.
$env:PYTHONNOUSERSITE = "1"
$env:PYTHONDONTWRITEBYTECODE = "1"
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

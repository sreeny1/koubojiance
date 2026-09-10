# Build a small app-only update package (app/ directory).
# ASCII-ONLY: PowerShell 5.1 on Chinese Windows parses BOM-less scripts as GBK.
param(
    [string]$Root = (Split-Path $PSScriptRoot -Parent),
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"

if (-not $Version) {
    $cfg = Get-Content (Join-Path $Root "app\core\config.py") -Raw -Encoding UTF8
    if ($cfg -match 'APP_VERSION\s*=\s*"([^"]+)"') { $Version = $Matches[1] }
}
if (-not $Version) { throw "Version not found. Pass -Version x.y.z" }

$OutDir = Join-Path $Root "build\updates"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$Zip = Join-Path $OutDir ("koubo-app-v" + $Version + ".zip")
$Stage = Join-Path $OutDir ("stage-app-" + $Version)
Remove-Item $Zip -Force -ErrorAction SilentlyContinue
Remove-Item $Stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $Stage | Out-Null

# Copy app/ into stage/app, excluding Python cache files.
robocopy (Join-Path $Root "app") (Join-Path $Stage "app") /E /XD __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy app failed: $LASTEXITCODE" }

Compress-Archive -Path (Join-Path $Stage "app") -DestinationPath $Zip -CompressionLevel Optimal -Force
Remove-Item $Stage -Recurse -Force -ErrorAction SilentlyContinue

$hash = (Get-FileHash $Zip -Algorithm SHA256).Hash.ToLower()
$size = (Get-Item $Zip).Length
$meta = [ordered]@{
    name = (Split-Path $Zip -Leaf)
    size = $size
    sha256 = $hash
    path = $Zip
    version = $Version
}
$meta | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $OutDir "app-update.json") -Encoding UTF8

Write-Host "APP UPDATE ZIP : $Zip"
Write-Host "SIZE           : $size"
Write-Host "SHA256         : $hash"

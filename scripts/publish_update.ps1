# Prepare an online-update release:
#   1. bump version in config.py / Launcher.cs / README.md
#   2. build app-only update zip
#   3. optionally build full portable zip (for first manual install)
#   4. write latest.json for the GitHub main branch
#   5. optionally commit, push and create GitHub Release with gh
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\publish_update.ps1 -Version 1.8.0 -Notes "fix xxx"
#   powershell ... -Version 1.8.0 -Notes "fix xxx" -Full
#   powershell ... -Version 1.8.0 -Notes "fix xxx" -Publish -Full
#
# ASCII-ONLY: PowerShell 5.1 on Chinese Windows parses BOM-less scripts as GBK.
param(
    [Parameter(Mandatory=$true)][string]$Version,
    [string]$Notes = "Bug fixes and improvements.",
    [switch]$Publish,
    [switch]$Full
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent

if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Version must be x.y.z, got: $Version"
}

function Write-Utf8NoBom([string]$Path, [string]$Text) {
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text, $enc)
}

function Update-FirstRegex([string]$Path, [string]$Pattern, [string]$Replacement) {
    $text = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    $re = New-Object System.Text.RegularExpressions.Regex($Pattern)
    if (-not $re.IsMatch($text)) { throw "Pattern not found in $Path : $Pattern" }
    $text = $re.Replace($text, $Replacement, 1)
    Write-Utf8NoBom $Path $text
}

# 1) read current values / repo
$configPath = Join-Path $Root "app\core\config.py"
$configText = [System.IO.File]::ReadAllText($configPath, [System.Text.Encoding]::UTF8)
$repo = "sreeny1/koubojiance"
if ($configText -match 'UPDATE_REPO\s*=\s*"([^"]+)"') { $repo = $Matches[1] }
$oldVersion = ""
if ($configText -match 'APP_VERSION\s*=\s*"([^"]+)"') { $oldVersion = $Matches[1] }

Write-Host "Bump version: $oldVersion -> $Version"
Update-FirstRegex $configPath 'APP_VERSION\s*=\s*"[^"]+"' "APP_VERSION = `"$Version`""
Update-FirstRegex (Join-Path $Root "tools\Launcher.cs") 'private const string Ver = "[^"]+";' "private const string Ver = `"$Version`";"
Update-FirstRegex (Join-Path $Root "tools\Launcher.cs") 'Program_Ver\(\) \{ return "[^"]+"; \}' "Program_Ver() { return `"$Version`"; }"
# README: only the first vX.Y.Z occurrence (the current-version banner at the top).
Update-FirstRegex (Join-Path $Root "README.md") 'v\d+\.\d+\.\d+' "v$Version"

# 2) build app-only zip
& (Join-Path $PSScriptRoot "make_app_update.ps1") -Root $Root -Version $Version
$metaPath = Join-Path $Root "build\updates\app-update.json"
$meta = Get-Content $metaPath -Raw -Encoding UTF8 | ConvertFrom-Json
$appUrl = "https://github.com/$repo/releases/download/v$Version/$($meta.name)"
$assets = [ordered]@{
    app = [ordered]@{
        name = [string]$meta.name
        url = $appUrl
        size = [int64]$meta.size
        sha256 = [string]$meta.sha256
    }
}
$assetPaths = @([string]$meta.path)

# 2b) optional full portable zip for first manual install
if ($Full) {
    & (Join-Path $PSScriptRoot "pack_full.ps1") -Root $Root
    $fullSource = Join-Path $Root "build\lianjinci-portable.zip"
    if (-not (Test-Path $fullSource)) { throw "full portable zip not found: $fullSource" }
    $fullName = "koubo-portable-v$Version.zip"
    $fullZip = Join-Path $Root ("build\updates\" + $fullName)
    Copy-Item $fullSource $fullZip -Force
    $fullHash = (Get-FileHash $fullZip -Algorithm SHA256).Hash.ToLower()
    $fullSize = (Get-Item $fullZip).Length
    $fullUrl = "https://github.com/$repo/releases/download/v$Version/$fullName"
    $assets["full"] = [ordered]@{
        name = $fullName
        url = $fullUrl
        size = [int64]$fullSize
        sha256 = $fullHash
    }
    $assetPaths += $fullZip
    Write-Host "FULL ZIP: $fullZip"
}

# 3) write latest.json
$latest = [ordered]@{
    version = $Version
    published_at = (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")
    notes = $Notes
    assets = $assets
}
$latestJson = $latest | ConvertTo-Json -Depth 8
Write-Utf8NoBom (Join-Path $Root "latest.json") ($latestJson + "`n")
Write-Host "latest.json updated: $appUrl"

$zipPath = [string]$meta.path
Write-Host "APP ZIP: $zipPath"

if (-not $Publish) {
    Write-Host ""
    Write-Host "Local files prepared. To publish, run this script again with -Publish."
    Write-Host "Required: git remote 'origin' configured and gh authenticated (gh auth login)."
    exit 0
}

# 4) commit / tag / push
Push-Location $Root
try {
    & git add app/core/config.py tools/Launcher.cs README.md latest.json
    & git commit -m "release: v$Version"
    $commitOk = ($LASTEXITCODE -eq 0)
    if (-not $commitOk) { Write-Host "No commit created (maybe nothing changed); continuing." }

    $tag = "v$Version"
    & git tag $tag 2>$null
    if ($LASTEXITCODE -ne 0) { Write-Host "Tag $tag already exists locally; trying to continue." }

    & git push origin main
    if ($LASTEXITCODE -ne 0) { throw "git push origin main failed" }
    & git push origin $tag
    if ($LASTEXITCODE -ne 0) { throw "git push origin $tag failed" }

    $gh = Get-Command gh -ErrorAction SilentlyContinue
    if (-not $gh) {
        $manual = Join-Path $Root "build\updates\PUBLISH_MANUAL.txt"
        @()
        "Git push done. One step remains: create the GitHub Release manually." | Set-Content $manual -Encoding UTF8
        "Repository : https://github.com/$repo" | Add-Content $manual -Encoding UTF8
        "Tag        : $tag" | Add-Content $manual -Encoding UTF8
        foreach ($p in $assetPaths) { "Asset      : $p" | Add-Content $manual -Encoding UTF8 }
        "Notes      : $Notes" | Add-Content $manual -Encoding UTF8
        Write-Host "gh not found. Manual instructions written to: $manual"
        exit 2
    }

    & gh release create $tag @assetPaths --repo $repo --title $tag --notes $Notes
    if ($LASTEXITCODE -ne 0) { throw "gh release create failed" }
    Write-Host "GitHub Release created: $tag"
} finally {
    Pop-Location
}

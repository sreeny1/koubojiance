# fetch_webview2.ps1
# Idempotent: downloads Microsoft.Web.WebView2 NuGet package and extracts
# WinForms/Core managed DLLs + native WebView2Loader (x64/x86) into lib\webview2.
#
# NOTE: keep this file ASCII-only (no Chinese chars) - on Chinese Windows,
# PowerShell 5.1 parses BOM-less .ps1 as GBK and non-ASCII strings break parsing.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\fetch_webview2.ps1
param(
    [string]$Version = "1.0.4129.50",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$Root   = Split-Path $PSScriptRoot -Parent
$Lib    = Join-Path $Root "lib\webview2"
$Marker = Join-Path $Lib ".fetched"

if ((Test-Path $Marker) -and -not $Force) {
    Write-Host "[webview2] ready (marker exists, use -Force to re-fetch)"
    exit 0
}

Write-Host "[webview2] downloading Microsoft.Web.WebView2 v$Version ..."
$nupkg = Join-Path $env:TEMP "Microsoft.Web.WebView2.$Version.nupkg"
Invoke-WebRequest -Uri "https://www.nuget.org/api/v2/package/Microsoft.Web.WebView2/$Version" -OutFile $nupkg -UseBasicParsing

# Expand-Archive only accepts .zip extension; copy nupkg to zip for extraction
$zip = [System.IO.Path]::ChangeExtension($nupkg, ".zip")
Copy-Item $nupkg $zip -Force

Write-Host "[webview2] extracting to $Lib ..."
if (Test-Path $Lib) { Remove-Item $Lib -Recurse -Force }
$Tmp = Join-Path $env:TEMP ("wv2_extract_" + $PID)
if (Test-Path $Tmp) { Remove-Item $Tmp -Recurse -Force }
Expand-Archive -Path $zip -DestinationPath $Tmp -Force

New-Item -ItemType Directory -Path $Lib -Force | Out-Null
Copy-Item (Join-Path $Tmp "lib\net462\Microsoft.Web.WebView2.Core.dll")     $Lib -Force
Copy-Item (Join-Path $Tmp "lib\net462\Microsoft.Web.WebView2.WinForms.dll") $Lib -Force
Copy-Item (Join-Path $Tmp "lib\net462\Microsoft.Web.WebView2.WPF.dll")      $Lib -Force

New-Item -ItemType Directory -Path (Join-Path $Lib "runtimes\win-x64\native") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $Lib "runtimes\win-x86\native") -Force | Out-Null
Copy-Item (Join-Path $Tmp "runtimes\win-x64\native\WebView2Loader.dll") (Join-Path $Lib "runtimes\win-x64\native") -Force
Copy-Item (Join-Path $Tmp "runtimes\win-x86\native\WebView2Loader.dll") (Join-Path $Lib "runtimes\win-x86\native") -Force

Remove-Item $Tmp -Recurse -Force
Set-Content -Path $Marker -Value $Version -Encoding UTF8

Write-Host "[webview2] done:"
Get-ChildItem $Lib -Recurse -File | ForEach-Object {
    $rel = $_.FullName.Replace($Root, '.')
    $kb = [math]::Round($_.Length / 1KB, 1)
    Write-Host ("  {0}  [{1} KB]" -f $rel, $kb)
}
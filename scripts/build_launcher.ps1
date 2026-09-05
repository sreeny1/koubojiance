# build_launcher.ps1
# Build the C# launcher (口播违禁词检测.exe) with WebView2 as a WinForms app.
# Keep this file ASCII-only (see fetch_webview2.ps1 note about Chinese Windows codepages).
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_launcher.ps1
param(
    [switch]$SkipFetch
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent

# Remove unused event to keep compile clean

# exe name "口播违禁词检测.exe" built from code points to survive GBK parsing
$exeName = ([string][char]0x53E3 + [string][char]0x64AD + [string][char]0x8FDD +
    [string][char]0x7981 + [string][char]0x8BCD + [string][char]0x68C0 +
    [string][char]0x6D4B + ".exe")
$Out = Join-Path $Root $exeName

# 1) ensure WebView2 deps
if (-not $SkipFetch) {
    & (Join-Path $PSScriptRoot "fetch_webview2.ps1")
    if ($LASTEXITCODE -ne 0) { throw "fetch_webview2 failed" }
}

$Wv2 = Join-Path $Root "lib\webview2"
foreach ($f in @("Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll")) {
    if (-not (Test-Path (Join-Path $Wv2 $f))) { throw "missing $f in lib\webview2" }
}

# 2) locate Framework csc.exe
$csc = "C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) {
    # fall back to 32-bit framework if x64 csc missing
    $csc = "C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe"
}
if (-not (Test-Path $csc)) { throw "csc.exe not found (.NET Framework 4.x SDK components missing)" }

# 3) compile
$icon = Join-Path $Root "data\brand\app_icon.ico"
$refs = @(
    "System.dll", "System.Drawing.dll", "System.Windows.Forms.dll", "System.Core.dll",
    "System.Management.dll",
    (Join-Path $Wv2 "Microsoft.Web.WebView2.Core.dll"),
    (Join-Path $Wv2 "Microsoft.Web.WebView2.WinForms.dll")
)
$argsList = @("/nologo", "/target:winexe")
foreach ($r in $refs) { $argsList += "/r:`"$r`"" }
$argsList += "/win32icon:`"$icon`""
$argsList += "/out:`"$Out`""
$argsList += (Join-Path $Root "tools\Launcher.cs")

$src = Join-Path $Root "tools\Launcher.cs"
Write-Host "[build] csc compiling ..."
& $csc $argsList
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Out)) {
    Write-Host "csc failed with exit code $LASTEXITCODE"
    exit 1
}

# 4) copy WebView2 managed + native DLLs next to the exe
Copy-Item (Join-Path $Wv2 "Microsoft.Web.WebView2.Core.dll")     (Join-Path $Root "Microsoft.Web.WebView2.Core.dll")     -Force
Copy-Item (Join-Path $Wv2 "Microsoft.Web.WebView2.WinForms.dll") (Join-Path $Root "Microsoft.Web.WebView2.WinForms.dll") -Force
Copy-Item (Join-Path $Wv2 "runtimes\win-x64\native\WebView2Loader.dll") (Join-Path $Root "WebView2Loader.dll") -Force
Copy-Item (Join-Path $Wv2 "runtimes\win-x86\native\WebView2Loader.dll") (Join-Path $Root "WebView2Loader_x86.dll") -Force
Write-Host "[build] WebView2 DLLs copied next to exe"

Write-Host "[build] OK -> $Out"
Write-Host ("[build] exe size: {0:N0} KB" -f ((Get-Item $Out).Length / 1KB))
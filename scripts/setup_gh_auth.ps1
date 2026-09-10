# One-time GitHub CLI authorization helper.
# After running this, the local GitHub CLI / git credential helper can be used
# by the AI assistant to push commits and publish releases without asking you
# to upload files manually.
#
# Token stays in Windows Credential Manager (via gh). It is never printed and
# never written to project files.
#
# Run:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup_gh_auth.ps1
#
# ASCII-ONLY for Windows PowerShell 5.1 on Chinese Windows.
param(
    [string]$Proxy = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent

if (-not $Proxy) {
    try {
        $Proxy = (& git -C $Root config --local --get http.proxy).Trim()
    } catch { $Proxy = "" }
    if (-not $Proxy) { $Proxy = "http://127.0.0.1:7890" }
}
$Proxy = $Proxy.TrimEnd("/")
$env:HTTPS_PROXY = $Proxy
$env:HTTP_PROXY = $Proxy

$gh = Join-Path $Root "build\tools\gh\bin\gh.exe"
if (-not (Test-Path $gh)) {
    $cmd = Get-Command gh -ErrorAction SilentlyContinue
    if ($cmd) { $gh = $cmd.Source }
}
if (-not (Test-Path $gh)) {
    throw "gh.exe not found. Ask the AI assistant to reinstall GitHub CLI first."
}

Write-Host "GitHub CLI : $gh"
Write-Host "Proxy      : $Proxy"
Write-Host ""
Write-Host "If you do not have a token yet:"
Write-Host "  1. Open https://github.com/settings/tokens/new"
Write-Host "  2. Note: local ai publish"
Write-Host "  3. Expiration: 30 days"
Write-Host "  4. Check the top-level 'repo' scope"
Write-Host "  5. Generate token and copy it"
Write-Host ""

$secure = Read-Host -AsSecureString "Paste GitHub token here (input hidden), then press Enter"
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    $token | & $gh auth login --hostname github.com --with-token
    if ($LASTEXITCODE -ne 0) { throw "gh auth login failed" }

    & $gh auth setup-git
    if ($LASTEXITCODE -ne 0) { throw "gh auth setup-git failed" }

    & $gh auth status
    if ($LASTEXITCODE -ne 0) { throw "gh auth status failed" }
} finally {
    if ($bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    Remove-Variable token -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "DONE. The AI assistant can now push and publish releases automatically."

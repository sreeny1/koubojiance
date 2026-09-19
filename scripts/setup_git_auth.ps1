# One-time Git-only credential setup.
# This bypasses GitHub CLI / api.github.com and stores the token in the
# Windows Credential Manager through git-credential-manager.
#
# Run:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup_git_auth.ps1
#
# ASCII-ONLY for Windows PowerShell 5.1 on Chinese Windows.
param(
    [string]$User = "sreeny1"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent

# Try to derive the username from the origin URL.
try {
    $url = (& git -C $Root remote get-url origin).Trim()
    if ($url -match 'github\.com[/:]([^/]+)/') { $User = $Matches[1] }
} catch { }

Write-Host "Git user : $User"
Write-Host "Repo     : " -NoNewline; (& git -C $Root remote get-url origin)
Write-Host ""
Write-Host "Create a classic token with the top-level 'repo' scope:"
Write-Host "  https://github.com/settings/tokens/new"
Write-Host ""

$secure = Read-Host -AsSecureString "Paste GitHub token here (input hidden), then press Enter"
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    $cred = "protocol=https`nhost=github.com`nusername=$User`npassword=$token`n`n"
    $cred | git -C $Root credential approve
    if ($LASTEXITCODE -ne 0) { throw "git credential approve failed" }

    Write-Host "Testing git authentication (no remote changes)..."
    $env:GIT_TERMINAL_PROMPT = "0"
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    & git -C $Root push --dry-run origin main 2>$null | Out-Null
    $pushCode = $LASTEXITCODE
    $ErrorActionPreference = $oldEap
    if ($pushCode -ne 0) { throw "git push authentication test failed" }
} finally {
    if ($bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    Remove-Variable token -ErrorAction SilentlyContinue
    Remove-Variable cred -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "DONE. Git push is now authorized on this machine."

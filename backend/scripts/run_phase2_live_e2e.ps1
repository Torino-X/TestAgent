[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://localhost:8000/api',
    [string]$Username = $env:PHASE2_E2E_USERNAME,
    [System.Security.SecureString]$Password,
    [switch]$EnableLiveCompaction,
    [string]$ConversationId,
    [switch]$SeedLiveConversation,
    [switch]$KeepWorkspaceArtifacts
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$backendRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $backendRoot '.venv\Scripts\python.exe'
$runner = Join-Path $PSScriptRoot 'phase2_live_api_e2e.py'
if (-not (Test-Path -LiteralPath $python)) { throw "Python virtual environment not found: $python" }
if (-not (Test-Path -LiteralPath $runner)) { throw "Live API runner not found: $runner" }
if (-not $Username) { $Username = Read-Host 'Phase 2 test account' }

$addedPassword = $false
if (-not $env:PHASE2_E2E_PASSWORD) {
    if (-not $Password) { $Password = Read-Host 'Phase 2 test password' -AsSecureString }
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Password)
    try { $env:PHASE2_E2E_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    $addedPassword = $true
}
$env:PHASE2_E2E_USERNAME = $Username

$arguments = @($runner, '--base-url', $BaseUrl)
if ($EnableLiveCompaction) { $arguments += '--enable-live-compaction' }
if ($ConversationId) { $arguments += @('--conversation-id', $ConversationId) }
if ($SeedLiveConversation) { $arguments += '--seed-live-conversation' }
if ($KeepWorkspaceArtifacts) { $arguments += '--keep-workspace-artifacts' }

Push-Location $backendRoot
try {
    & $python @arguments
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
    Remove-Item Env:PHASE2_E2E_USERNAME -ErrorAction SilentlyContinue
    if ($addedPassword) { Remove-Item Env:PHASE2_E2E_PASSWORD -ErrorAction SilentlyContinue }
}
exit $exitCode

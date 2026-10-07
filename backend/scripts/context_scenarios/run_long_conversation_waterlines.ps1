[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://localhost:8000/api',
    [string]$Username = $env:PHASE2_E2E_USERNAME,
    [System.Security.SecureString]$Password,
    [ValidateRange(50, 150)][int]$WarmupTurns = 50,
    [ValidateRange(99, 150)][int]$MaxTurns = 150,
    [ValidateRange(12000, 105000)][int]$MaxPayloadChars = 105000,
    [switch]$RouteProbeOnly,
    [switch]$KeepArtifacts,
    [switch]$KeepLatestPassedConversation,
    [switch]$CheckpointAfterHardDiagnostic,
    [switch]$ResumeHardCheckpoint,
    [switch]$FreshHardProbe,
    [ValidateRange(1800, 105000)][int]$HardProbePayloadChars = 83140,
    [ValidateRange(1, 4)][int]$HardProbeLoadTurns = 3
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$backendRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$python = Join-Path $backendRoot '.venv\Scripts\python.exe'
$runner = Join-Path $PSScriptRoot 'long_conversation_waterlines.py'
if (-not (Test-Path -LiteralPath $python)) { throw "Python virtual environment not found: $python" }
if (-not (Test-Path -LiteralPath $runner)) { throw "Scenario runner not found: $runner" }
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

$arguments = @($runner, '--base-url', $BaseUrl, '--warmup-turns', $WarmupTurns, '--max-turns', $MaxTurns, '--max-payload-chars', $MaxPayloadChars)
if ($KeepArtifacts) { $arguments += '--keep-artifacts' }
if ($KeepLatestPassedConversation) { $arguments += '--retain-latest-passed-conversation' }
if ($RouteProbeOnly) { $arguments += '--route-probe-only' }
if ($CheckpointAfterHardDiagnostic) { $arguments += '--checkpoint-after-hard-diagnostic' }
if ($ResumeHardCheckpoint) { $arguments += '--resume-hard-checkpoint' }
if ($FreshHardProbe) {
    $arguments += '--fresh-hard-probe'
    $arguments += '--hard-probe-payload-chars'
    $arguments += $HardProbePayloadChars
    $arguments += '--hard-probe-load-turns'
    $arguments += $HardProbeLoadTurns
}

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

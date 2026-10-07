[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://localhost:8000/api',
    [string]$Username = $env:PHASE2_E2E_USERNAME,
    [System.Security.SecureString]$Password,
    [ValidateRange(20, 600)][int]$RequestTimeoutSeconds = 180,
    [ValidateRange(3, 900)][int]$IndexWaitSeconds = 180
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$backendRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$python = Join-Path $backendRoot '.venv\Scripts\python.exe'
$runner = Join-Path $PSScriptRoot 'project_refund_long_memory.py'
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

Push-Location $backendRoot
try {
    & $python $runner '--base-url' $BaseUrl '--request-timeout-seconds' $RequestTimeoutSeconds '--index-wait-seconds' $IndexWaitSeconds
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
    Remove-Item Env:PHASE2_E2E_USERNAME -ErrorAction SilentlyContinue
    if ($addedPassword) { Remove-Item Env:PHASE2_E2E_PASSWORD -ErrorAction SilentlyContinue }
}
exit $exitCode

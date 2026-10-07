[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://localhost:8000/api',
    [string]$Username = $env:PHASE2_E2E_USERNAME,
    [System.Security.SecureString]$Password,
    [ValidateRange(20, 600)][int]$RequestTimeoutSeconds = 240,
    [ValidateRange(3, 900)][int]$IndexWaitSeconds = 300,
    [ValidateRange(30, 1800)][int]$TaskWaitSeconds = 900,
    [ValidateRange(1, 48)][double]$PreparePercent = 40,
    [ValidateRange(2, 49.9)][double]$TargetPercent = 45,
    [ValidateRange(5, 30)][int]$MaxTurns = 30,
    [ValidateRange(2000, 50000)][int]$MaxPromptChars = 30000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($PreparePercent -ge $TargetPercent) {
    throw 'PreparePercent must be lower than TargetPercent.'
}

$backendRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $backendRoot '.venv\Scripts\python.exe'
$runner = Join-Path $PSScriptRoot 'context_scenarios\project_context_pressure_50.py'

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment not found: $python"
}
if (-not (Test-Path -LiteralPath $runner)) {
    throw "Scenario runner not found: $runner"
}
$addedUsername = $false
if (-not $Username) {
    $Username = Read-Host 'Phase 2 test account'
    $addedUsername = $true
}

$addedPassword = $false
if (-not $env:PHASE2_E2E_PASSWORD) {
    if (-not $Password) {
        $Password = Read-Host 'Phase 2 test password' -AsSecureString
    }
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Password)
    try {
        $env:PHASE2_E2E_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    $addedPassword = $true
}
$env:PHASE2_E2E_USERNAME = $Username

$runnerArgs = @(
    '--base-url', $BaseUrl,
    '--request-timeout-seconds', $RequestTimeoutSeconds,
    '--index-wait-seconds', $IndexWaitSeconds,
    '--task-wait-seconds', $TaskWaitSeconds,
    '--prepare-percent', $PreparePercent,
    '--target-percent', $TargetPercent,
    '--max-turns', $MaxTurns,
    '--max-prompt-chars', $MaxPromptChars
)

Push-Location $backendRoot
try {
    & $python $runner @runnerArgs
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
    if ($addedUsername) {
        Remove-Item Env:PHASE2_E2E_USERNAME -ErrorAction SilentlyContinue
    }
    if ($addedPassword) {
        Remove-Item Env:PHASE2_E2E_PASSWORD -ErrorAction SilentlyContinue
    }
}

exit $exitCode

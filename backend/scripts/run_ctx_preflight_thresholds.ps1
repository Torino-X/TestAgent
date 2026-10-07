[CmdletBinding()]
param(
    [switch]$Resume,
    [string]$AnalysisFile,
    [switch]$RetryAbsolute
)

$ErrorActionPreference = 'Stop'
$backendRoot = Split-Path -Parent $PSScriptRoot
$script = Join-Path $PSScriptRoot 'context_scenarios\preflight_threshold_lifecycle.py'
$python = Join-Path $backendRoot '.venv\Scripts\python.exe'
$arguments = @()
if ($Resume) { $arguments += '--resume' }
if ($AnalysisFile) { $arguments += @('--analysis-file', $AnalysisFile) }
if ($RetryAbsolute) { $arguments += '--retry-absolute' }
Push-Location $backendRoot
try {
    & $python $script @arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}

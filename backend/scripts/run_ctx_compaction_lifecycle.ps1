[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://localhost:8000/api',
    [ValidateRange(30, 900)][int]$RequestTimeoutSeconds = 240,
    [ValidateRange(20, 2000)][int]$MaxTurnsPerRound = 600,
    [ValidateRange(1, 2)][int]$MaxRounds = 2,
    [ValidateRange(30, 900)][int]$CompactionWaitSeconds = 180,
    [switch]$Resume,
    [string]$AnalysisFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($AnalysisFile -and -not $Resume) { throw '-AnalysisFile requires -Resume.' }
$backendRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $backendRoot '.venv\Scripts\python.exe'
$runner = Join-Path $PSScriptRoot 'context_scenarios\conversation_compaction_lifecycle.py'
if (-not (Test-Path -LiteralPath $python)) { throw "Python virtual environment not found: $python" }
if (-not (Test-Path -LiteralPath $runner)) { throw "Scenario runner not found: $runner" }
if (-not $env:PHASE2_E2E_USERNAME -or -not $env:PHASE2_E2E_PASSWORD) { throw 'PHASE2_E2E_USERNAME and PHASE2_E2E_PASSWORD must be set.' }
    $runnerArgs = @('--base-url', $BaseUrl, '--request-timeout-seconds', $RequestTimeoutSeconds, '--max-turns-per-round', $MaxTurnsPerRound, '--max-rounds', $MaxRounds, '--compaction-wait-seconds', $CompactionWaitSeconds)
if ($Resume) { $runnerArgs += '--resume' }
if ($AnalysisFile) { $runnerArgs += @('--analysis-file', $AnalysisFile) }
Push-Location $backendRoot
try { & $python $runner @runnerArgs; exit $LASTEXITCODE } finally { Pop-Location }

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('LCT-01', 'LCT-02', 'LCT-03', 'LCT-04', 'LCT-05', 'LCT-06')]
    [string]$Lct
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$backendRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $backendRoot '.venv\Scripts\python.exe'
$runner = Join-Path $PSScriptRoot 'phase2_resource_pack_runner.py'
if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment not found: $python. Create backend/.venv before running the scenario."
}
if (-not (Test-Path -LiteralPath $runner)) {
    throw "Standalone resource-pack runner not found: $runner"
}

# This is deliberately not a pytest wrapper.  The Python runner constructs a
# fresh scenario for this LCT and writes report.json, steps.json, execution.log
# and any failure traceback under test-results/phase2-resource-pack/.
Push-Location $backendRoot
try {
    & $python $runner --lct $Lct
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $exitCode

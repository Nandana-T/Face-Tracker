# Runs every quality gate. Stops at the first failure. Usage: .\scripts\check.ps1 [-SkipE2E]
param([switch]$SkipE2E)
. "$PSScriptRoot\env.ps1"
$ErrorActionPreference = 'Stop'

function Step($name, [scriptblock]$cmd) {
    Write-Host "`n=== $name ===" -ForegroundColor Cyan
    & $cmd
    if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: $name" -ForegroundColor Red; exit $LASTEXITCODE }
}

Push-Location (Join-Path $Root 'backend')
Step 'backend: ruff check'  { & $Py -m ruff check . }
Step 'backend: ruff format' { & $Py -m ruff format --check . }
Step 'backend: mypy'        { & $Py -m mypy }
Step 'backend: pytest'      { & $Py -m pytest -q }
Pop-Location

Push-Location (Join-Path $Root 'frontend')
Step 'frontend: eslint'    { npx eslint . }
Step 'frontend: tsc'       { npx tsc --noEmit }
Step 'frontend: vitest'    { npx vitest run }
Step 'frontend: build'     { npx vite build }
if (-not $SkipE2E) {
    if (-not $env:PW_CHANNEL) { $env:PW_CHANNEL = 'msedge' }
    Step 'frontend: playwright' { npx playwright test }
}
Pop-Location
Write-Host "`nAll checks passed." -ForegroundColor Green

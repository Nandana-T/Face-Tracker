# Runs every quality gate. Stops at the first failure.
#   .\scripts\check.ps1 [-SkipE2E] [-UseConfiguredDatabase]
#
# Tests run against the embedded local PostgreSQL (scripts\local-db.ps1) by default, so they never
# touch the hosted database. -UseConfiguredDatabase opts in to TEST_DATABASE_URL / DATABASE_URL.
param([switch]$SkipE2E, [switch]$UseConfiguredDatabase)
. "$PSScriptRoot\env.ps1"
$ErrorActionPreference = 'Stop'

function Step($name, [scriptblock]$cmd) {
    Write-Host "`n=== $name ===" -ForegroundColor Cyan
    & $cmd
    if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: $name" -ForegroundColor Red; exit $LASTEXITCODE }
}

if (-not $UseConfiguredDatabase) {
    $env:TEST_DATABASE_URL = (& $Py (Join-Path $Root 'scripts\local_pg.py') start)
    Write-Host "Using embedded local PostgreSQL for tests."
}

Step 'model: present and checksum-verified' { Push-Location (Join-Path $Root 'backend'); & $Py -m app.vision.detection.model_registry verify; $c = $LASTEXITCODE; Pop-Location; if ($c) { throw "Run scripts\download-model.ps1" } }

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
    # The E2E suite starts its own API + worker + dev server (ports 8000 and 5173 must be free).
    Step 'frontend: playwright' { npx playwright test }
}
Pop-Location
Write-Host "`nAll checks passed." -ForegroundColor Green

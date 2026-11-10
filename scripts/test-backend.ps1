# Usage: .\scripts\test-backend.ps1 [-Unit]   (-Unit skips tests that need a real PostgreSQL)
param([switch]$Unit)
. "$PSScriptRoot\env.ps1"
Push-Location (Join-Path $Root 'backend')
try {
    if ($Unit) { & $Py -m pytest -q tests/unit } else { & $Py -m pytest -q }
} finally { Pop-Location }
exit $LASTEXITCODE

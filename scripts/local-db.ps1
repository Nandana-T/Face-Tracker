# Embedded local PostgreSQL (stored in storage\pgdata on D:). Development/test fallback that needs
# no Docker and no cloud credentials.
#   .\scripts\local-db.ps1 start   -> starts it, prints the URL, sets $env:DATABASE_URL and
#                                     $env:TEST_DATABASE_URL for this shell (dot-source to keep them:
#                                     . .\scripts\local-db.ps1 start)
#   .\scripts\local-db.ps1 stop
param([ValidateSet('start', 'stop')][string]$Action = 'start')
. "$PSScriptRoot\env.ps1"
$result = & $Py (Join-Path $Root 'scripts\local_pg.py') $Action
if ($Action -eq 'start') {
    $env:DATABASE_URL = $result
    $env:TEST_DATABASE_URL = $result
    Write-Host "Local PostgreSQL running (DATABASE_URL / TEST_DATABASE_URL set for this shell)."
    Write-Host "Apply migrations with: .\scripts\migrate.ps1"
} else { Write-Host $result }

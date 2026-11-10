# Applies Alembic migrations to DATABASE_URL.  Usage: .\scripts\migrate.ps1 [target]   (default: head)
param([string]$Target = 'head')
. "$PSScriptRoot\env.ps1"
Push-Location (Join-Path $Root 'backend')
try { & $Py -m alembic upgrade $Target } finally { Pop-Location }
exit $LASTEXITCODE

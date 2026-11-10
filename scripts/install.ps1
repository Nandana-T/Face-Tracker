# Installs all dependencies. Python (3.12) + venv + caches stay inside the project directory.
# Requires: uv (https://docs.astral.sh/uv/) - either on PATH or at .tools\uv\bin\uv.exe - and Node 20+.
. "$PSScriptRoot\env.ps1"
$ErrorActionPreference = 'Stop'
if (-not $Uv) { throw "uv not found. Install with: pip install --target .tools\uv uv   (see README)" }

Push-Location (Join-Path $Root 'backend')
& $Uv python install 3.12
& $Uv sync --frozen
Pop-Location

Push-Location (Join-Path $Root 'frontend')
npm ci
Pop-Location

if (-not (Test-Path (Join-Path $Root '.env'))) {
    Copy-Item (Join-Path $Root '.env.example') (Join-Path $Root '.env')
    Write-Host "Created .env from .env.example - edit DATABASE_URL before migrating."
}
Write-Host "Install complete."

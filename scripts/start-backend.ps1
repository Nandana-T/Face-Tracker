. "$PSScriptRoot\env.ps1"
Push-Location (Join-Path $Root 'backend')
try { & $Py -m uvicorn app.main:app_factory --factory --host 127.0.0.1 --port 8000 --reload } finally { Pop-Location }

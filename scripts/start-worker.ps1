. "$PSScriptRoot\env.ps1"
Push-Location (Join-Path $Root 'backend')
try { & $Py -m app.workers.video_worker } finally { Pop-Location }

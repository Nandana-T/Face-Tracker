. "$PSScriptRoot\env.ps1"
Push-Location (Join-Path $Root 'frontend')
try { npm run dev } finally { Pop-Location }

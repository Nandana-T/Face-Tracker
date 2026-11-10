# Usage: .\scripts\test-frontend.ps1 [-E2E]   (E2E needs Edge/Chrome via PW_CHANNEL, default msedge)
param([switch]$E2E)
. "$PSScriptRoot\env.ps1"
Push-Location (Join-Path $Root 'frontend')
try {
    if ($E2E) {
        if (-not $env:PW_CHANNEL) { $env:PW_CHANNEL = 'msedge' }
        npx playwright test
    } else { npm test }
} finally { Pop-Location }
exit $LASTEXITCODE

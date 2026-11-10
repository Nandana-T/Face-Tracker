# Downloads the pinned YOLO face-detection weights into models\face-detector (on D:), verifying the
# SHA-256. This is the ONLY place weights are fetched - the API and the worker never download.
#   .\scripts\download-model.ps1 [-Force]
param([switch]$Force)
. "$PSScriptRoot\env.ps1"
Push-Location (Join-Path $Root 'backend')
try {
    $args2 = @('-m', 'app.vision.detection.model_registry', 'fetch')
    if ($Force) { $args2 += '--force' }
    & $Py @args2
} finally { Pop-Location }
exit $LASTEXITCODE

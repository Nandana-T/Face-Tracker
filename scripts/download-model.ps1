# Downloads pinned model weights onto D: (models\), verifying the SHA-256. This is the ONLY place weights
# are fetched - the API and the worker never download anything.
#   .\scripts\download-model.ps1 [-Force]        YOLO face detector -> models\face-detector
#   .\scripts\download-model.ps1 -Recognition    SFace embedder + YuNet landmarks -> models\face-recognizer
# Licences and limits of every model: docs\decisions\0007-face-detection-model.md,
# 0011-model-licence-assessment.md and 0013-recognition-model-and-licensing.md (non-commercial scope).
param([switch]$Force, [switch]$Recognition)
. "$PSScriptRoot\env.ps1"
Push-Location (Join-Path $Root 'backend')
try {
    if ($Recognition) {
        & $Py -m app.vision.recognition.registry fetch
    } else {
        $args2 = @('-m', 'app.vision.detection.model_registry', 'fetch')
        if ($Force) { $args2 += '--force' }
        & $Py @args2
    }
} finally { Pop-Location }
exit $LASTEXITCODE

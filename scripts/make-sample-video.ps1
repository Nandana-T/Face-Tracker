# Generates a small SYNTHETIC test video (a public-domain portrait panned and zoomed over a plain
# background) into datasets\samples so it appears in the Live Monitoring source list. It exercises
# the pipeline with a real face but is not real footage and says nothing about accuracy.
#   .\scripts\make-sample-video.ps1 [-Seconds 8] [-Faces 1]
param([int]$Seconds = 8, [ValidateSet(1, 2)][int]$Faces = 1, [string]$Name = 'synthetic-portrait.mp4')
. "$PSScriptRoot\env.ps1"
Push-Location (Join-Path $Root 'backend')
try {
    & $Py -m app.vision.sample_video --out (Join-Path $Root "datasets\samples\$Name") --seconds $Seconds --faces $Faces
} finally { Pop-Location }
exit $LASTEXITCODE

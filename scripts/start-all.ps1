# Runs the API and the video worker (two separate processes) under one supervisor, plus nothing
# else - start the dashboard with .\scripts\start-frontend.ps1 in a second terminal.
#   .\scripts\start-all.ps1
. "$PSScriptRoot\env.ps1"
& $Py (Join-Path $Root 'scripts\dev_stack.py')
exit $LASTEXITCODE

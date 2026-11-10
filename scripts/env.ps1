# Dot-source this file:  . .\scripts\env.ps1
# Points every cache and tool at the project directory (D: in this setup). No global settings are modified;
# values apply to the current PowerShell session only.
$script:Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$env:KATOMARAN_ROOT = $Root
$env:UV_CACHE_DIR = Join-Path $Root 'cache\uv'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Root '.tools\python'
$env:UV_PROJECT_ENVIRONMENT = Join-Path $Root '.venv'
$env:PIP_CACHE_DIR = Join-Path $Root 'cache\pip'
$env:npm_config_cache = Join-Path $Root 'cache\npm'
$env:HF_HOME = Join-Path $Root 'cache\huggingface'
$env:TORCH_HOME = Join-Path $Root 'cache\torch'
$env:INSIGHTFACE_HOME = Join-Path $Root 'cache\insightface'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $Root 'cache\playwright'
$env:TEMP = Join-Path $Root 'temp'
$env:TMP = $env:TEMP
foreach ($d in 'cache\uv','cache\pip','cache\npm','cache\huggingface','cache\torch','cache\insightface','cache\playwright','temp','models','datasets','storage','logs\entries','logs\exits') {
    New-Item -ItemType Directory -Force (Join-Path $Root $d) | Out-Null
}
$script:Uv = Join-Path $Root '.tools\uv\bin\uv.exe'
if (-not (Test-Path $Uv)) { $script:Uv = (Get-Command uv -ErrorAction SilentlyContinue).Source }
$script:Py = Join-Path $Root '.venv\Scripts\python.exe'

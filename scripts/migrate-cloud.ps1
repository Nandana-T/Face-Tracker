# Applies Alembic migrations to the HOSTED database - only after the credential was rotated.
#   .\scripts\migrate-cloud.ps1            -> checks the credential, connects, upgrades to head, verifies
#   .\scripts\migrate-cloud.ps1 -CheckOnly -> checks only (no connection)
# Refuses to run (exit 3) while .env still contains the password that was shared in chat.
# Safe to repeat: `alembic upgrade head` is idempotent and migrations never drop existing data.
param([switch]$CheckOnly)
. "$PSScriptRoot\env.ps1"
$extra = @()
if (-not $CheckOnly) { $extra = @('--migrate') }
& $Py (Join-Path $Root 'scripts\db_credential_check.py') @extra
exit $LASTEXITCODE

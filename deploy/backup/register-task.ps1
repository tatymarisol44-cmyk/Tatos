# Registers the daily encrypted backup as a Windows scheduled task (docs/runbooks/backup.md).
# It runs every day at the given time; if the computer was off, it runs when it starts.
#
#   powershell -ExecutionPolicy Bypass -File deploy\backup\register-task.ps1
#
param(
    [string]$Secrets = "$env:USERPROFILE\agency-render.env",
    [string]$At = "13:00"
)

$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$python = Join-Path $repo ".venv\Scripts\python.exe"
$script = Join-Path $repo "deploy\backup\run_backup.py"
if (-not (Test-Path $python)) { throw "Python environment not found: $python (run: uv sync)" }
if (-not (Test-Path $Secrets)) { throw "Private file not found: $Secrets" }

$action = New-ScheduledTaskAction -Execute $python -Argument "`"$script`" `"$Secrets`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName "Agency backup" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Encrypted backup of the practice's database (agency-orchestrator)" -Force | Out-Null
Write-Output "Scheduled: every day at $At (or when the computer starts, if it was off)."

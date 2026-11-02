# Age of AI — start the live world automatically when you log on to Windows (version 6).
# Creates a scheduled task "Age of AI" for the current user (no admin rights needed).
# Remove it again with: powershell -ExecutionPolicy Bypass -File scripts\uninstall_autostart.ps1

$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root "scripts\start_world.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "Age of AI" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Keeps the Age of AI world running (http://localhost:8000)." -Force | Out-Null
Write-Host "Done: the world starts when you log on. Start it now with: Start-ScheduledTask -TaskName 'Age of AI'"

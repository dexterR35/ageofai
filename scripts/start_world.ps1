# Age of AI — keeps the live world running. If the server stops or crashes, it starts again after 10 s
# and the world continues from its last save (the missing minutes are replayed in fast mode).
# Started at logon by the scheduled task from install_autostart.ps1, or by hand:
#   powershell -ExecutionPolicy Bypass -File scripts\start_world.ps1

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$logs = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force $logs | Out-Null
Set-Location $root

while ($true) {
    $log = Join-Path $logs ("world_" + (Get-Date -Format "yyyy-MM-dd") + ".log")
    Add-Content -Path $log -Encoding utf8 -Value ("=== start " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
    & $python cli.py serve *>> $log
    Add-Content -Path $log -Encoding utf8 -Value ("=== stopped with code $LASTEXITCODE at " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + "; restarting in 10 s")
    Start-Sleep -Seconds 10
}

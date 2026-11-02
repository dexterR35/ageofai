# Age of AI — stop the live world: the keep-running loop, the game server, and the night's training with its
# worker processes. The world was saved within the last minute; when you start it again it continues from there
# and replays the missing time.
#   powershell -ExecutionPolicy Bypass -File scripts\stop_world.ps1

$all = Get-CimInstance Win32_Process
$mine = $all | Where-Object {
    $_.CommandLine -and $_.CommandLine -notmatch 'stop_world\.ps1' -and $_.CommandLine -notmatch 'Get-CimInstance' -and
    ($_.CommandLine -match 'start_world\.ps1' -or $_.CommandLine -match 'cli\.py"?\s+(serve|evolve)')
}
$ids = @($mine | ForEach-Object { $_.ProcessId })
# the training's worker processes are children of `cli.py evolve`
$workers = $all | Where-Object { $ids -contains $_.ParentProcessId -and $_.CommandLine -match 'multiprocessing' }
foreach ($p in $mine | Where-Object { $_.CommandLine -match 'start_world\.ps1' }) {   # the loop first (no restart)
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
foreach ($p in @($mine | Where-Object { $_.CommandLine -notmatch 'start_world\.ps1' }) + @($workers)) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
Write-Host "Stopped $(@($mine).Count + @($workers).Count) process(es). Start the world again with scripts\start_world.ps1"

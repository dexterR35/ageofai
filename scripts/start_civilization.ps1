# Separate continuous world: four civilizations, normally five real minutes per game hour.
param([int]$Port = 8000)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
& (Join-Path $projectRoot ".venv\Scripts\python.exe") cli.py serve --profile civilization --port $Port
exit $LASTEXITCODE

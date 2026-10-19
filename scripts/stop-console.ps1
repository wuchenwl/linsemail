$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $projectRoot '.runtime'
Set-Location $projectRoot
& (Join-Path $projectRoot '.venv\Scripts\python.exe') -X utf8 scripts/console-services.py stop
if ($LASTEXITCODE -ne 0) { throw 'Console shutdown failed.' }

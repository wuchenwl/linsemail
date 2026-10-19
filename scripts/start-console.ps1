param([switch]$SkipInstall, [switch]$SkipBuild)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $projectRoot '.runtime'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
Set-Location $projectRoot
$node = (Get-Command node -ErrorAction Stop).Source
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $python)) {
    & python -m venv (Join-Path $projectRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python virtual environment.' }
}
if (!$SkipInstall) {
    & $python -m pip install -r backend/requirements.lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
    & npm.cmd ci --prefix frontend --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
    & npm.cmd ci --prefix tools/postgres --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL dependency installation failed.' }
}
if (!$SkipBuild) {
    & npm.cmd run build --prefix frontend
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
}
& $python -X utf8 scripts/console-services.py start
if ($LASTEXITCODE -ne 0) { throw 'Console startup failed; inspect .runtime/*.log.' }

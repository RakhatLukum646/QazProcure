$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    throw 'Сначала выполните start.ps1 для установки окружения.'
}
$logPath = Join-Path $PSScriptRoot 'data\worker.log'
New-Item -ItemType Directory -Force -Path (Split-Path $logPath) | Out-Null
& '.\.venv\Scripts\python.exe' -u -m app.cli worker >> $logPath 2>&1
exit $LASTEXITCODE

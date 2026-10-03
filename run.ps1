$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    Write-Error 'Run setup.ps1 first.'
    exit 1
}
& '.\.venv\Scripts\python.exe' '.\login.py' @args
exit $LASTEXITCODE

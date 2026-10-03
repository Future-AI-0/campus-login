param([switch]$SingleFile, [switch]$CompactDirectory)
$ErrorActionPreference = 'Stop'
if ($SingleFile -and $CompactDirectory) { throw 'Choose one build layout.' }
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$taskRelease = if ($SingleFile) { Join-Path $PSScriptRoot 'dist\Standalone' } elseif ($CompactDirectory) { Join-Path $PSScriptRoot 'dist\CampusLoginSlim' } else { Join-Path $PSScriptRoot 'dist\CampusLogin' }
$taskPrivateConfig = @{}
foreach ($taskConfigName in @('.env', 'settings.json')) {
    $taskConfigPath = Join-Path $taskRelease $taskConfigName
    if (Test-Path -LiteralPath $taskConfigPath -PathType Leaf) {
        $taskPrivateConfig[$taskConfigName] = [IO.File]::ReadAllBytes($taskConfigPath)
    }
}
if (-not (Test-Path -LiteralPath $taskPython)) {
    Write-Error 'Run setup.ps1 first.'
    exit 1
}
& $taskPython -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
# 隔离 PATH，避免其他工具（例如 Poppler）的同名 ICU DLL 被误打包。
$taskOriginalPath = $env:PATH
$taskPythonBase = & $taskPython -c 'import sys; print(sys.base_prefix)'
try {
    $env:PATH = "$(Join-Path $PSScriptRoot '.venv\Scripts');$taskPythonBase;$env:SystemRoot\System32;$env:SystemRoot"
    if ($SingleFile) {
        & $taskPython -m PyInstaller --noconfirm --clean --distpath $taskRelease CampusLogin-slim.spec
    } elseif ($CompactDirectory) {
        & $taskPython -m PyInstaller --noconfirm --clean --distpath (Join-Path $PSScriptRoot 'dist') CampusLogin-slim.spec -- --directory
    } else {
        & $taskPython -m PyInstaller --noconfirm --clean --windowed --onedir --name CampusLogin gui.py
    }
    $taskBuildExitCode = $LASTEXITCODE
} finally {
    $env:PATH = $taskOriginalPath
}
foreach ($taskConfigName in $taskPrivateConfig.Keys) {
    if (-not (Test-Path -LiteralPath $taskRelease -PathType Container)) {
        New-Item -ItemType Directory -Path $taskRelease -Force | Out-Null
    }
    [IO.File]::WriteAllBytes((Join-Path $taskRelease $taskConfigName), $taskPrivateConfig[$taskConfigName])
}
if ($taskBuildExitCode -ne 0) { exit $taskBuildExitCode }
if ($SingleFile) {
    Copy-Item -LiteralPath '使用说明-单文件版.txt' -Destination (Join-Path $taskRelease '使用说明.txt') -Force
    $taskZipFiles = @((Join-Path $taskRelease 'CampusLogin.exe'), (Join-Path $taskRelease '使用说明.txt'))
    Compress-Archive -LiteralPath $taskZipFiles -DestinationPath (Join-Path $PSScriptRoot 'dist\CampusLogin-OneFile.zip') -Force
    Write-Host 'Built: dist\Standalone\CampusLogin.exe'
    Write-Host 'Portable package: dist\CampusLogin-OneFile.zip'
    exit 0
}
if ($CompactDirectory) {
    Copy-Item -LiteralPath '使用说明.txt' -Destination (Join-Path $taskRelease '使用说明.txt') -Force
    $taskZipFiles = @((Join-Path $taskRelease 'CampusLogin.exe'), (Join-Path $taskRelease '_internal'), (Join-Path $taskRelease '使用说明.txt'))
    Compress-Archive -LiteralPath $taskZipFiles -DestinationPath (Join-Path $PSScriptRoot 'dist\CampusLogin-Slim.zip') -Force
    Write-Host 'Built: dist\CampusLoginSlim\CampusLogin.exe'
    Write-Host 'Portable package: dist\CampusLogin-Slim.zip'
    exit 0
}
# Python 3.10 自带的旧 VC runtime 可能先于 Qt 加载；统一为 Qt wheel 的版本。
$taskQtRuntime = Join-Path $PSScriptRoot '.venv\Lib\site-packages\PySide6'
foreach ($taskRuntimeName in @('vcruntime140.dll', 'vcruntime140_1.dll')) {
    Copy-Item -LiteralPath (Join-Path $taskQtRuntime $taskRuntimeName) -Destination (Join-Path $taskRelease "_internal\$taskRuntimeName") -Force
}
Copy-Item -LiteralPath '.env.example' -Destination (Join-Path $taskRelease '.env.example')
Copy-Item -LiteralPath '使用说明.txt' -Destination (Join-Path $taskRelease '使用说明.txt')
# 只使用明确列出的发布文件，避免把个人 .env 带入 ZIP。
$taskZipFiles = @(
    (Join-Path $taskRelease 'CampusLogin.exe'),
    (Join-Path $taskRelease '_internal'),
    (Join-Path $taskRelease '.env.example'),
    (Join-Path $taskRelease '使用说明.txt')
)
Compress-Archive -LiteralPath $taskZipFiles -DestinationPath (Join-Path $PSScriptRoot 'dist\CampusLogin-Windows.zip') -Force
Write-Host 'Built: dist\CampusLogin\CampusLogin.exe'
Write-Host 'Portable package: dist\CampusLogin-Windows.zip'

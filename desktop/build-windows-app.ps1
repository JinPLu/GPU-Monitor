$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir
$specPath = Join-Path $scriptDir "windows\GPUBrokerWindows.spec"
$outputRoot = Join-Path $projectRoot "dist\windows"
$workPath = Join-Path $projectRoot "build\windows"

if ($env:OS -ne "Windows_NT") {
    throw "Windows desktop builds must run on Windows because PyInstaller cannot cross-compile a Windows .exe."
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required. Install it first, then rerun this script."
}

Set-Location $projectRoot

uv sync --extra dev --reinstall-package gpu-broker

if (Test-Path $outputRoot) {
    Remove-Item -Recurse -Force $outputRoot
}
New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null

uv run --with pyinstaller pyinstaller `
    --clean `
    --noconfirm `
    --distpath $outputRoot `
    --workpath $workPath `
    $specPath

$appPath = Join-Path $outputRoot "GPU Broker\GPU Broker.exe"
if (-not (Test-Path $appPath)) {
    throw "Build finished but $appPath was not created."
}

Write-Host "Built $appPath"

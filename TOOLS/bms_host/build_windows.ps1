$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Venv = Join-Path $ProjectRoot ".venv-bms-host-build"
$Python = Join-Path $Venv "Scripts\python.exe"

if (-not (Test-Path $Python)) {
    py -3.11 -m venv $Venv
}

& $Python -m pip install --upgrade pip
& $Python -m pip install -r (Join-Path $PSScriptRoot "requirements-build.txt")
Push-Location $ProjectRoot
try {
    & $Python -m unittest Tests.test_bms_host_protocol -v
    & $Python -m PyInstaller --noconfirm --clean (Join-Path $PSScriptRoot "bms_control_desk.spec")
}
finally {
    Pop-Location
}

Write-Host "Build complete: $ProjectRoot\dist\BITFSAE_BMS_Control_Desk"

$ErrorActionPreference = "Stop"

# Windows PowerShell 对 python 等原生命令的非零退出码默认不中止，
# 必须逐步检查 $LASTEXITCODE，否则测试失败仍会继续打包并返回成功。
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Venv = Join-Path $ProjectRoot ".venv-bms-host-build"
$Python = Join-Path $Venv "Scripts\python.exe"

if (-not (Test-Path $Python)) {
    py -3.11 -m venv $Venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

& $Python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Python -m pip install -r (Join-Path $PSScriptRoot "requirements-build.txt")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Push-Location $ProjectRoot
try {
    & $Python -m unittest discover -s Tests -p "test_bms_host*.py" -v
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $Python -m PyInstaller --noconfirm --clean (Join-Path $PSScriptRoot "bms_control_desk.spec")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally {
    Pop-Location
}

Write-Host "Build complete: $ProjectRoot\dist\BITFSAE_BMS_Control_Desk"

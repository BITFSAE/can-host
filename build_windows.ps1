param(
    # 发布产物命名标签：默认取 canhost/__init__.py 的 __version__，
    # CI 打标签构建时传入 v 标签（如 v0.8.3），手动触发时传短 SHA。
    [string]$Label = "",
    # CI 传入：缺少 Inno Setup 时直接失败；本地构建缺 ISCC 只警告并跳过安装包。
    [switch]$RequireInstaller
)

$ErrorActionPreference = "Stop"

# Windows PowerShell 对 python 等原生命令的非零退出码默认不中止，
# 必须逐步检查 $LASTEXITCODE，否则测试失败仍会继续打包并返回成功。
$ProjectRoot = $PSScriptRoot
$Venv = Join-Path $ProjectRoot ".venv-canhost-build"
$Python = Join-Path $Venv "Scripts\python.exe"

if (-not (Test-Path $Python)) {
    py -3.11 -m venv $Venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

& $Python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Python -m pip install -r (Join-Path $ProjectRoot "requirements-build.txt")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Push-Location $ProjectRoot
try {
    & $Python -m unittest discover -s Tests -p "test_*.py" -v
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $Python -m PyInstaller --noconfirm --clean (Join-Path $ProjectRoot "can_host.spec")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    if (-not $Label) {
        $Label = (& $Python -c "from canhost import __version__; print(__version__)").Trim()
        if ($LASTEXITCODE -ne 0 -or -not $Label) {
            Write-Error "无法读取 canhost.__version__ 作为发布标签。"
            exit 1
        }
    }
    # 统一去掉开头的 v（CI 传标签 v0.8.3，__version__ 是 0.8.3），
    # 产物命名统一为 BITFSAE_CAN_Host_v<版本>.*，与软件内更新器附件约定一致。
    if ($Label.StartsWith("v") -or $Label.StartsWith("V")) { $Label = $Label.Substring(1) }

    # 发布产物命名与软件内更新器约定一致（canhost/updater.py）：
    # ZIP 内层必须是 BITFSAE_CAN_Host/ 目录，附件名 BITFSAE_CAN_Host_v<标签>.zip 与 .sha256。
    $ReleaseDir = Join-Path $ProjectRoot "release"
    New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null
    $Base = "BITFSAE_CAN_Host_v$Label"
    $ZipPath = Join-Path $ReleaseDir "$Base.zip"
    if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
    Compress-Archive -Path (Join-Path $ProjectRoot "dist\BITFSAE_CAN_Host") -DestinationPath $ZipPath
    $Hash = (Get-FileHash -Algorithm SHA256 -Path $ZipPath).Hash
    "$Hash  $Base.zip" | Set-Content -Encoding ascii (Join-Path $ReleaseDir "$Base.zip.sha256")

    # Inno Setup 安装包：VersionInfoVersion 只接受数字版本，标签带 -rc1 等
    # 后缀时拆出主体；非数字标签（手动触发的短 SHA）回落为 0.0.0。
    $VersionBase = $Label.Split('-')[0]
    if ($VersionBase -notmatch '^\d+\.\d+\.\d+$') { $VersionBase = "0.0.0" }
    $Iscc = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
    if (-not (Test-Path $Iscc)) {
        $Candidate = Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"
        if (Test-Path $Candidate) { $Iscc = $Candidate }
    }
    if (Test-Path $Iscc) {
        & $Iscc "/DMyAppVersion=$VersionBase" "/DMyAppVersionLabel=$Label" `
            "/DIconFile=$(Join-Path $ProjectRoot 'app_icon.ico')" `
            "/DSourceDir=$(Join-Path $ProjectRoot 'dist\BITFSAE_CAN_Host')" `
            "/DOutputDir=$ReleaseDir" `
            (Join-Path $ProjectRoot "packaging\windows\canhost.iss")
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    } elseif ($RequireInstaller) {
        Write-Error "未找到 Inno Setup 6（ISCC.exe），无法生成安装包；安装 Inno Setup 6 后重试。"
        exit 1
    } else {
        Write-Warning "未找到 Inno Setup 6，跳过安装包生成，仅输出 ZIP 与 SHA256 校验文件。"
    }
}
finally {
    Pop-Location
}

Write-Host "Build complete: $ProjectRoot\release"

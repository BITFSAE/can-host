"""CNB 镜像优先、GitHub 回退的发布更新器（冻结版 Windows 上位机使用）。

国内网络直连 GitHub 不可靠，发布产物因此同时镜像到 CNB（cnb.cool）：
检查更新先读 CNB 上匿名可读的更新频道，失败才回退 GitHub API；
下载地址由所选发布自带的附件地址决定，因此不需要代理，也不需要任何令牌。

HTTPS 请求统一走 ``trust.https_ssl_context()``：保留系统证书库并追加随包的
certifi 证书，避免 macOS 冻结包缺少系统 CA 时检查更新报
CERTIFICATE_VERIFY_FAILED。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from . import trust
from .release_notes import release_notes_from_body


DEFAULT_REPO = "BITFSAE/can-host"
# 国内镜像仓库：cnb.cool 的公开仓库，发布附件与更新频道都允许匿名读取。
DEFAULT_CNB_REPO = "totok22/can-host"
CNB_WEB_BASE = "https://cnb.cool"
# 更新频道由 CI 写在独立分支上，避免与 GitHub main 的代码镜像互相覆盖。
CNB_CHANNEL_BRANCH = "cnb-update"
CNB_CHANNEL_PATH = "latest.json"
SOURCE_CNB = "cnb"
SOURCE_GITHUB = "github"
# 顺序即优先级：CNB 镜像优先，失败回退 GitHub。
DEFAULT_SOURCES = (SOURCE_CNB, SOURCE_GITHUB)
SOURCE_LABELS = {SOURCE_CNB: "CNB 镜像", SOURCE_GITHUB: "GitHub"}
# 只向这些域名发送已保存的 GitHub 令牌，其余（含 CNB 与预签名地址）一律匿名。
GITHUB_HOSTS = ("github.com", "githubusercontent.com")

# 保留系统信任库，同时追加随包 certifi；macOS 冻结包缺少系统 CA 时也能校验。
SSL_CONTEXT = trust.https_ssl_context()

APP_FOLDER_NAME = "BITFSAE_CAN_Host"
APP_EXE_NAME = f"{APP_FOLDER_NAME}.exe"
ASSET_PATTERN = re.compile(rf"^{APP_FOLDER_NAME}_v.+\\.zip$", re.IGNORECASE)
CHECKSUM_PATTERN = re.compile(rf"^{APP_FOLDER_NAME}_v.+\\.zip\\.sha256$", re.IGNORECASE)
SHA256_LINE = re.compile(r"(?m)^\s*([0-9a-fA-F]{64})")

SETTINGS_DIR_NAME = "BITFSAE"
SETTINGS_SUBDIR_NAME = "CAN Host"

# ``BITFSAE_CAN_Host.old-<yyyyMMddHHmmss>`` written by the install helper.
BACKUP_DIR_PATTERN = re.compile(rf"^{APP_FOLDER_NAME}\.old-(\d{{14}})$")
# The helper waits up to 90 s for the old process and validates the new one for
# about 8 s; backups younger than this may still be a rollback target, so the
# startup cleanup must leave them alone.
BACKUP_KEEP_SECONDS = 15 * 60

# The helper is hidden, so diagnostics must live outside its disposable
# staging directory. Otherwise cleanup removes the only useful failure record.
UPDATE_LOG_DIR_NAME = "update-logs"
UPDATE_RESULT_NAME = "last-update-result.json"
# 上一次成功启动的版本，用来判断这次启动是不是软件内更新或安装包升级后的首启。
INSTALLED_VERSION_NAME = "installed-version.json"


def is_github_url(url: str) -> bool:
    """True only for GitHub-hosted URLs, the sole place the saved token may go."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if not host:
        return False
    return any(host == item or host.endswith("." + item) for item in GITHUB_HOSTS)


def cnb_channel_url(
    repo: str = DEFAULT_CNB_REPO,
    base: str = CNB_WEB_BASE,
    branch: str = CNB_CHANNEL_BRANCH,
    path: str = CNB_CHANNEL_PATH,
) -> str:
    """Anonymous raw address of the CNB update channel written by CI."""
    return "{}/{}/-/git/raw/{}/{}".format(
        base.rstrip("/"),
        repo.strip("/"),
        urllib.parse.quote(branch, safe=""),
        urllib.parse.quote(path, safe=""),
    )


def _user_settings_dir() -> Path:
    """Per-user directory for the updater token and small settings."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / SETTINGS_DIR_NAME / SETTINGS_SUBDIR_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "can-host"


def settings_path() -> Path:
    return _user_settings_dir() / "settings.json"


def _write_settings(payload: dict[str, Any]) -> None:
    """Atomically persist the updater settings file."""
    target = settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix="settings-", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "nt":
            try:
                os.replace(temp_name, target)
            except OSError:
                if target.exists():
                    target.unlink()
                os.replace(temp_name, target)
        else:
            os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _read_settings() -> dict[str, Any]:
    target = settings_path()
    try:
        with target.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def _valid_update_token(token: str | None) -> str | None:
    if not token:
        return None
    value = token.strip()
    return value if len(value) <= 512 else None


def install_ready() -> bool:
    """Only the frozen Windows build may replace its own installation."""
    return bool(getattr(sys, "frozen", False) and os.name == "nt")


INSTALLER_SCRIPT = r'''
param(
    [Parameter(Mandatory=$true)][string]$AppDir,
    [Parameter(Mandatory=$true)][string]$StagedDir,
    [Parameter(Mandatory=$true)][string]$ExeName,
    [Parameter(Mandatory=$true)][string]$WorkDir,
    [Parameter(Mandatory=$true)][string]$ExpectedVersion,
    [Parameter(Mandatory=$true)][string]$LogPath,
    [Parameter(Mandatory=$true)][string]$HealthFile,
    [Parameter(Mandatory=$true)][string]$ResultPath,
    [int]$OldPid = 0
)
$ErrorActionPreference = "Stop"
function Write-Log([string]$Message) {
    try {
        $stamp = [DateTime]::UtcNow.ToString("o")
        Add-Content -LiteralPath $LogPath -Value "$stamp $Message" -Encoding UTF8
    } catch {}
}
function Write-Result([string]$Code) {
    try {
        $payload = [ordered]@{
            ok = $false
            code = $Code
            log_path = $LogPath
            at = [DateTime]::UtcNow.ToString("o")
        }
        $payload | ConvertTo-Json -Compress | Set-Content -LiteralPath $ResultPath -Encoding UTF8
    } catch {}
}
function Show-Failure() {
    try {
        Add-Type -AssemblyName PresentationFramework
        $message = "The update failed. The previous version was restored when possible.`n`nLog: $LogPath"
        [System.Windows.MessageBox]::Show($message, "BITFSAE CAN Host update", "OK", "Error") | Out-Null
    } catch {}
}
function Start-App([string]$Directory, [bool]$WithHealth) {
    $target = Join-Path $Directory $ExeName
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
        throw "application executable missing: $target"
    }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $target
    $psi.WorkingDirectory = $Directory
    $psi.UseShellExecute = $true
    $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Normal
    if ($WithHealth) {
        $psi.Arguments = "--update-health-file `"$HealthFile`""
    }
    return [System.Diagnostics.Process]::Start($psi)
}
function Rename-DirectoryWithRetry([string]$Source, [string]$DestinationLeaf) {
    $attempt = 1
    while ($true) {
        try {
            Rename-Item -LiteralPath $Source -NewName $DestinationLeaf -ErrorAction Stop
            return
        } catch {
            if ($attempt -ge 30) { throw }
            if ($attempt -eq 1) { Write-Log "old rename blocked; retrying" }
            $attempt += 1
            Start-Sleep -Milliseconds 500
        }
    }
}
Write-Log "start staged=$StagedDir app=$AppDir"

$stagedExe = Join-Path $StagedDir $ExeName
if (-not (Test-Path -LiteralPath $stagedExe -PathType Leaf)) {
    Write-Log "staged exe missing"
    Write-Result "staged_exe_missing"
    Show-Failure
    exit 21
}

if ($OldPid -gt 0) {
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        $process = Get-Process -Id $OldPid -ErrorAction SilentlyContinue
        if (-not $process) { break }
        Start-Sleep -Milliseconds 250
    }
    if (Get-Process -Id $OldPid -ErrorAction SilentlyContinue) {
        Write-Log "old process did not exit gracefully; forcing stop pid=$OldPid"
        Stop-Process -Id $OldPid -Force -ErrorAction SilentlyContinue
        try { Wait-Process -Id $OldPid -Timeout 10 -ErrorAction SilentlyContinue } catch {}
    }
    if (Get-Process -Id $OldPid -ErrorAction SilentlyContinue) {
        Write-Log "old process still running after forced stop"
        Write-Result "old_process_stuck"
        Show-Failure
        exit 22
    }
}

$backupPrefix = "$(Split-Path $AppDir -Leaf).old-"
$backupDir = Split-Path $AppDir -Parent
$backup = "$AppDir.old-$([DateTime]::UtcNow.ToString('yyyyMMddHHmmss'))"

$movedOld = $false
$new = $null
$failureCode = "install_failed"
try {
    Rename-DirectoryWithRetry $AppDir (Split-Path $backup -Leaf)
    $movedOld = $true
    Write-Log "old renamed $backup"
    Move-Item -LiteralPath $StagedDir -Destination $AppDir
    Write-Log "new moved"

    # The Inno Setup uninstaller is not part of the update ZIP. Copy it back
    # so the Windows uninstall entry remains valid. Portable installs have none.
    foreach ($unins in @("unins000.exe", "unins000.dat")) {
        $src = Join-Path $backup $unins
        if (Test-Path -LiteralPath $src -PathType Leaf) {
            try {
                Copy-Item -LiteralPath $src -Destination (Join-Path $AppDir $unins) -Force
                Write-Log "uninstaller preserved $unins"
            } catch {
                Write-Log "uninstaller copy failed ${unins}: $_"
                $failureCode = "uninstaller_preserve_failed"
                throw
            }
        }
    }

    Remove-Item -LiteralPath $HealthFile -Force -ErrorAction SilentlyContinue
    $new = Start-App $AppDir $true
    Write-Log "new process started pid=$($new.Id)"

    $deadline = (Get-Date).AddSeconds(45)
    $ok = $false
    while ((Get-Date) -lt $deadline) {
        $new.Refresh()
        if ($new.HasExited) {
            Write-Log "new process exited early code=$($new.ExitCode)"
            $failureCode = "new_process_exited"
            break
        }
        if (Test-Path -LiteralPath $HealthFile -PathType Leaf) {
            try {
                $health = Get-Content -LiteralPath $HealthFile -Raw | ConvertFrom-Json
                $actualVersion = [string]$health.version
                # AppVersion contains the numeric build version. Release tags
                # may add -rc/-pre labels, so compare their numeric base.
                $expected = (($ExpectedVersion -replace '^[vV]', '') -split '-')[0]
                if ([int]$health.pid -ne $new.Id) { throw "health pid mismatch" }
                if ($expected -and $actualVersion -ne $expected) { throw "health version mismatch" }
                $ok = $true
                Write-Log "frontend healthy pid=$($new.Id) version=$actualVersion"
                break
            } catch {
                Write-Log "health signal invalid: $_"
                $failureCode = "health_mismatch"
                break
            }
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $ok) {
        if ($failureCode -eq "install_failed") { $failureCode = "health_timeout" }
        throw "new application did not report healthy"
    }

    Write-Log "install success pid=$($new.Id)"
    $stale = Get-ChildItem -LiteralPath $backupDir -Filter "$backupPrefix*" -Directory -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -Skip 1
    foreach ($item in $stale) {
        try { Remove-Item -LiteralPath $item.FullName -Recurse -Force -ErrorAction Stop } catch { Write-Log "stale cleanup failed $($item.FullName)" }
    }
    try { Remove-Item -LiteralPath $WorkDir -Recurse -Force -ErrorAction Stop } catch { Write-Log "work cleanup failed" }
    exit 0
} catch {
    Write-Log "install failed: $_"
    if ($new -and -not $new.HasExited) {
        try {
            Stop-Process -Id $new.Id -Force -ErrorAction Stop
            Wait-Process -Id $new.Id -Timeout 10 -ErrorAction SilentlyContinue
            Write-Log "failed new process stopped pid=$($new.Id)"
        } catch {
            Write-Log "failed new process could not be stopped: $_"
        }
    }
    $restored = $false
    if ($movedOld -and (Test-Path -LiteralPath $backup)) {
        try {
            if (Test-Path -LiteralPath $AppDir) { Remove-Item -LiteralPath $AppDir -Recurse -Force -ErrorAction Stop }
            Move-Item -LiteralPath $backup -Destination $AppDir
            Write-Log "old restored"
            $restored = $true
        } catch {
            Write-Log "restore failed: $_"
            $failureCode = "restore_failed"
        }
    } elseif (-not $movedOld -and (Test-Path -LiteralPath $AppDir)) {
        $restored = $true
    }
    if ($restored) {
        try {
            $old = Start-App $AppDir $false
            Write-Log "old process restarted pid=$($old.Id)"
        } catch {
            Write-Log "old process restart failed: $_"
            $failureCode = "restart_failed"
        }
    }
    Write-Result $failureCode
    Show-Failure
    exit 23
}
'''


def update_log_dir() -> Path:
    """Persistent updater diagnostics directory, outside disposable staging."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / SETTINGS_DIR_NAME / SETTINGS_SUBDIR_NAME / UPDATE_LOG_DIR_NAME
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "can-host" / UPDATE_LOG_DIR_NAME


def update_result_path() -> Path:
    """One-shot result read by the app restarted after a rollback."""
    return _user_settings_dir() / UPDATE_RESULT_NAME


def installed_version_path() -> Path:
    """Marker holding the version that last started successfully."""
    return _user_settings_dir() / INSTALLED_VERSION_NAME


def release_page_url(version: str, repo: str = DEFAULT_REPO) -> str:
    """Version-specific GitHub Release page, used as the update-notes fallback."""
    tag = f"v{str(version).strip().lstrip('vV')}" if str(version).strip() else ""
    return f"https://github.com/{repo}/releases/tag/{tag}" if tag else f"https://github.com/{repo}/releases"


def changelog_page_url(repo: str = DEFAULT_REPO) -> str:
    return f"https://github.com/{repo}/blob/main/CHANGELOG.md"


def remember_release_record(latest: dict[str, Any]) -> None:
    """Persist the checked release notes so the next launch can show them.

    The updater only knows the release body while it is online; the update
    dialog that runs after an in-app restart must still be able to show what
    changed without another network round trip.
    """
    record = {
        "tag_name": str(latest.get("tag_name") or ""),
        "name": str(latest.get("name") or ""),
        "published_at": str(latest.get("published_at") or ""),
        "html_url": str(latest.get("html_url") or ""),
        "notes": [str(item) for item in (latest.get("changes") or [])][:40],
        "at": datetime.now(timezone.utc).isoformat(),
    }
    payload = _read_settings()
    payload["last_release"] = record
    try:
        _write_settings(payload)
    except OSError:
        pass


def release_record() -> dict[str, Any]:
    """Last checked release record, including its extracted notes."""
    payload = _read_settings().get("last_release")
    return payload if isinstance(payload, dict) else {}


def release_record_for(version: str) -> dict[str, Any]:
    """Release record that belongs to ``version``; otherwise an empty dict."""
    record = release_record()
    tag = str(record.get("tag_name") or "").lstrip("vV")
    wanted = str(version or "").strip().lstrip("vV").split("-")[0]
    if not tag or not wanted or tag.split("-")[0] != wanted:
        return {}
    return record


# 更新弹窗的历史版本列表只用于阅读说明，正文按下面的长度截断，避免每次轮询
# 都把十份完整 Release 正文重新发过 JSBridge。
HISTORY_LIMIT = 8
HISTORY_BODY_CHARS = 4000


def _history_summary(release: dict[str, Any]) -> dict[str, Any]:
    summary = _release_summary(release)
    return {
        "tag_name": summary["tag_name"],
        "name": summary["name"],
        "html_url": summary["html_url"],
        "published_at": summary["published_at"],
        "prerelease": summary["prerelease"],
        "changes": summary["changes"],
        "body": summary["body"][:HISTORY_BODY_CHARS],
    }


def record_installed_version(version: str) -> None:
    """Remember which version started, so the next upgrade can report the change.

    Best effort: a failure here only costs a future update dialog, never startup.
    """
    target = installed_version_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": str(version), "at": datetime.now(timezone.utc).isoformat()}
        temporary = target.with_name(f".{target.name}-{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, target)
    except OSError:
        pass


def previous_installed_version() -> str:
    """Version recorded by the last successful start, or an empty string."""
    try:
        payload = json.loads(installed_version_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(payload.get("version") or "") if isinstance(payload, dict) else ""


def installed_update_state(
    current_version: str,
    version_date: str = "",
) -> dict[str, Any]:
    """Report what this launch changed, for both in-app updates and installers.

    Read-only: the marker file is committed by ``Api.mark_frontend_ready`` once
    the UI has actually loaded, so a build that dies during startup cannot claim
    the upgrade was completed and silence the next launch's report.
    """
    from . import release_notes

    previous = previous_installed_version()
    try:
        upgraded = bool(previous) and release_is_newer(current_version, previous)
    except ValueError:
        previous, upgraded = "", False
    notes: list[str] = []
    source = ""
    record = release_record_for(current_version)
    if not previous or upgraded:
        cached = [str(item) for item in (record.get("notes") or [])]
        notes, source = release_notes.notes_for_release(current_version, cached)
    return {
        "upgraded": upgraded,
        "first_launch": not previous,
        "previous_version": previous,
        "current_version": str(current_version),
        "version_date": release_notes.release_date_for(current_version) or version_date,
        "notes": notes,
        "notes_source": source,
        "release_name": str(record.get("name") or ""),
        "published_at": str(record.get("published_at") or ""),
        "release_url": release_page_url(current_version),
        "changelog_url": changelog_page_url(),
    }


UPDATE_FAILURE_MESSAGES = {
    "staged_exe_missing": "更新包不完整，已保留当前版本",
    "old_process_stuck": "旧版本无法退出，更新已取消",
    "new_process_exited": "新版本启动失败，已自动恢复并启动旧版本",
    "health_timeout": "新版本界面未能正常启动，已自动恢复并启动旧版本",
    "health_mismatch": "新版本启动确认不匹配，已自动恢复并启动旧版本",
    "uninstaller_preserve_failed": "无法保留 Windows 卸载入口，已自动恢复并启动旧版本",
    "restore_failed": "更新失败，且旧版本文件恢复失败",
    "restart_failed": "已恢复旧版本文件，但未能自动重新打开",
    "install_failed": "安装更新失败，已自动恢复并启动旧版本",
}


def consume_update_result() -> dict[str, Any] | None:
    """Read and remove a helper failure so the rollback is reported once."""
    target = update_result_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return None
    finally:
        try:
            target.unlink()
        except OSError:
            pass
    if not isinstance(payload, dict) or payload.get("ok") is not False:
        return None
    code = str(payload.get("code") or "install_failed")
    return {
        "ok": False,
        "code": code,
        "message": UPDATE_FAILURE_MESSAGES.get(code, UPDATE_FAILURE_MESSAGES["install_failed"]),
        "log_path": str(payload.get("log_path") or ""),
    }


def _powershell() -> Path:
    if os.name != "nt":
        raise RuntimeError("安装助手仅支持 Windows")
    return Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def launch_installer(
    app_dir: Path,
    stage_dir: Path,
    work_dir: Path,
    expected_version: str,
    exe_name: str = APP_EXE_NAME,
    current_pid: int | None = None,
) -> Path:
    """Write a hidden PowerShell helper and start it detached from the app."""
    if not install_ready():
        raise RuntimeError("源码运行只支持检查更新，不能替换安装目录")
    if not stage_dir.is_absolute() or not work_dir.is_absolute():
        raise ValueError("安装目录必须是绝对路径")
    if not app_dir.is_absolute():
        raise ValueError("应用目录必须是绝对路径")
    if not (stage_dir / exe_name).is_file():
        raise ValueError(f"已下载的更新包缺少 {exe_name}")
    script_path = work_dir / "install-helper.ps1"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(INSTALLER_SCRIPT, encoding="utf-8")
    log_dir = update_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        existing_logs = sorted(
            log_dir.glob("update-*.log"), key=lambda item: item.stat().st_mtime, reverse=True
        )
    except OSError:
        existing_logs = []
    for stale_log in existing_logs[19:]:
        try:
            stale_log.unlink()
        except OSError:
            pass
    log_path = log_dir / f"update-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}.log"
    health_file = work_dir / "update-health.json"
    result_path = update_result_path()
    result_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        result_path.unlink()
    except OSError:
        pass
    command = [
        str(_powershell()),
        "-NoProfile",
        "-NonInteractive",
        "-WindowStyle", "Hidden",
        "-ExecutionPolicy", "Bypass",
        "-File", str(script_path),
        "-AppDir", str(app_dir),
        "-StagedDir", str(stage_dir),
        "-ExeName", exe_name,
        "-WorkDir", str(work_dir),
        "-ExpectedVersion", expected_version,
        "-LogPath", str(log_path),
        "-HealthFile", str(health_file),
        "-ResultPath", str(result_path),
        "-OldPid", str(current_pid or os.getpid()),
    ]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        # Installed shortcuts start the app with cwd=app_dir.  The detached
        # PowerShell helper must not inherit that directory or its own current
        # directory handle prevents Rename-Item from swapping the installation.
        # Use the update directory's parent so the helper can also remove
        # work_dir after a successful install.
        "cwd": str(work_dir.parent.resolve()),
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(command, **kwargs)
    return script_path


def _pre_key(value: str | None) -> tuple:
    if value is None:
        return (2,)
    parts: list[tuple[int, int | str]] = []
    for part in re.split(r"(\d+)", value):
        if not part:
            continue
        if part.isdigit():
            parts.append((0, int(part)))
        else:
            parts.append((1, part.lower()))
    return (1, tuple(parts))

def version_key(tag: str) -> tuple[int, int, int, tuple]:
    """Parse ``vX.Y.Z`` or ``vX.Y.Z-pre`` into a deterministic sort key."""
    text = tag[1:] if tag.startswith(("v", "V")) else tag
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z][0-9A-Za-z.-]*))?", text)
    if not match:
        raise ValueError(f"无法识别 GitHub 版本标签：{tag}")
    major, minor, patch = (int(match.group(index)) for index in (1, 2, 3))
    return major, minor, patch, _pre_key(match.group(4))

def release_is_newer(remote_tag: str, current_version: str) -> bool:
    """True when a release tag is strictly newer than the local version."""
    return version_key(remote_tag) > version_key(current_version)

def _release_summary(release: dict[str, Any]) -> dict[str, Any]:
    body = str(release.get("body") or "")
    return {
        "tag_name": str(release.get("tag_name") or ""),
        "name": str(release.get("name") or ""),
        "html_url": str(release.get("html_url") or ""),
        "published_at": str(release.get("published_at") or ""),
        "prerelease": bool(release.get("prerelease", False)),
        "body": body[:12000],
        # The dialog lists these entries directly; the raw body stays for the
        # technical panel where the full release text is still useful.
        "changes": release_notes_from_body(body),
        "assets": [
            {
                "id": asset.get("id"),
                "name": str(asset.get("name") or ""),
                "size": int(asset.get("size") or 0),
                "url": str(asset.get("url") or ""),
                "browser_download_url": str(asset.get("browser_download_url") or ""),
            }
            for asset in release.get("assets") or []
            if asset.get("name") and asset.get("url")
        ],
    }

def find_zip_asset(release: dict[str, Any]) -> dict[str, Any] | None:
    """Return the Windows one-folder asset belonging to a release."""
    tag = str(release.get("tag_name") or "")
    expected = f"{APP_FOLDER_NAME}_{tag}.zip"
    assets = release.get("assets") or []
    for asset in assets:
        if str(asset.get("name") or "").lower() == expected.lower():
            return asset
    for asset in assets:
        if ASSET_PATTERN.match(str(asset.get("name") or "")):
            return asset
    return None

def find_checksum_asset(release: dict[str, Any], zip_name: str) -> dict[str, Any] | None:
    expected = f"{zip_name}.sha256"
    assets = release.get("assets") or []
    for asset in assets:
        if str(asset.get("name") or "").lower() == expected.lower():
            return asset
    for asset in assets:
        if CHECKSUM_PATTERN.match(str(asset.get("name") or "")):
            return asset
    return None

def read_sha256_digest(path: Path) -> str:
    """Read the first 64 hex digits from a GitHub checksum asset."""
    content = path.read_text(encoding="utf-8", errors="replace")
    match = SHA256_LINE.search(content)
    if not match:
        raise ValueError(f"校验文件 {path.name} 中没有 SHA-256")
    return match.group(1).lower()

def _safe_member(top_level: str, member: zipfile.ZipInfo) -> str:
    raw = member.filename.replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise ValueError(f"发布包包含绝对路径：{member.filename}")
    if "//" in raw:
        raise ValueError(f"发布包包含不规范路径：{member.filename}")
    normalized = raw.rstrip("/")
    parts = PurePosixPath(normalized).parts
    if not parts or parts[0] != top_level or any(part in ("..", "") for part in parts):
        raise ValueError(f"发布包包含不安全路径：{member.filename}")
    if (member.external_attr >> 16) & 0xF000 == 0xA000:
        raise ValueError(f"发布包包含符号链接：{member.filename}")
    return normalized

def extract_update_archive(zip_path: Path, destination: Path, exe_name: str = APP_EXE_NAME) -> Path:
    """Validate and extract a safe one-folder update archive."""
    destination.mkdir(parents=True, exist_ok=True)
    full_exe = f"{APP_FOLDER_NAME}/{exe_name}"
    has_exe = False
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            normalized = _safe_member(APP_FOLDER_NAME, member)
            if normalized.lower() == full_exe.lower():
                has_exe = True
        archive.extractall(destination)
    result = destination / APP_FOLDER_NAME
    if not has_exe or not (result / exe_name).is_file():
        raise ValueError(f"发布包缺少 {APP_FOLDER_NAME}\\{exe_name}")
    return result

def update_temp_dir(parent: Path, suffix: str = "") -> Path:
    unique = f"{int(time.time() * 1000)}-{os.getpid()}-{suffix}"
    return parent / f"canhost-update-{unique}"

def cleanup_update_dirs(parent: Path, keep: set[Path] | None = None) -> int:
    """Remove stale ``canhost-update-*`` directories."""
    keep = keep or set()
    removed = 0
    try:
        for child in parent.iterdir():
            if child.is_dir() and child.name.startswith("canhost-update-") and child not in keep:
                try:
                    for item in sorted(child.rglob("*"), reverse=True):
                        try:
                            if item.is_file() or item.is_symlink():
                                item.unlink()
                            else:
                                item.rmdir()
                        except OSError:
                            pass
                    child.rmdir()
                    removed += 1
                except OSError:
                    pass
    except OSError:
        pass
    return removed


def cleanup_old_backups(parent: Path, now: float | None = None, keep_seconds: int = BACKUP_KEEP_SECONDS) -> int:
    """Delete ``BITFSAE_CAN_Host.old-<timestamp>`` backups past the rollback window.

    The install helper deliberately keeps the newest backup for manual rollback;
    the updated build deletes it on its next startup, once it has demonstrably
    been able to run.  Best effort: unreadable or busy directories are skipped.
    """
    current = time.time() if now is None else now
    removed = 0
    try:
        children = sorted(parent.iterdir())
    except OSError:
        return 0
    for child in children:
        match = BACKUP_DIR_PATTERN.match(child.name)
        if not match or not child.is_dir():
            continue
        try:
            stamp = datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if current - stamp.timestamp() < keep_seconds:
            continue
        shutil.rmtree(child, ignore_errors=True)
        if not child.exists():
            removed += 1
    return removed


def startup_cleanup(
    app_dir: Path,
    now: float | None = None,
    keep_temp_dirs: set[Path] | None = None,
) -> dict[str, int]:
    """Remove update leftovers when the frozen app starts.

    Deletes sibling old-version backups past the rollback window and stale
    ``canhost-update-*`` temp directories so old versions do not accumulate.
    """
    removed_backups = cleanup_old_backups(app_dir.parent, now=now)
    temp_parent = Path(os.environ.get("TEMP") or tempfile.gettempdir())
    removed_temp = cleanup_update_dirs(temp_parent, keep=keep_temp_dirs)
    return {"old_backups": removed_backups, "temp_dirs": removed_temp}


class HostUpdater:
    """Background check/download state machine for CNB and GitHub releases."""

    def __init__(
        self,
        repo: str = DEFAULT_REPO,
        current_version: str = "0.0.0",
        token_provider: Callable[[], str | None] | None = None,
        timeout: float = 20.0,
        cnb_repo: str = DEFAULT_CNB_REPO,
        sources: Iterable[str] = DEFAULT_SOURCES,
    ) -> None:
        self.repo = repo
        self.cnb_repo = cnb_repo
        self.sources = tuple(sources) or DEFAULT_SOURCES
        self.current_version = current_version
        self._token_provider = token_provider or (lambda: None)
        self.timeout = timeout
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "state": "idle",
            "message": "尚未检查更新",
            "error": None,
            "progress": 0.0,
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "download_speed_bps": 0.0,
            "download_stage": "",
            "latest": None,
            "history": [],
            "source": None,
            "downloaded_zip": "",
            "stage_dir": "",
            "checked_at": None,
            "include_prerelease": False,
            "installing": False,
            "install_error": None,
        }

    def set_token(self, token: str) -> dict[str, Any]:
        """Persist a GitHub token used only by the updater backend."""
        value = _valid_update_token(token)
        if value is None:
            return {"ok": False, "error": "令牌格式无效"}
        payload = _read_settings()
        payload["github_token"] = value
        try:
            _write_settings(payload)
        except OSError as exc:
            return {"ok": False, "error": f"无法保存令牌：{exc}"}
        return {"ok": True}

    def clear_token(self) -> dict[str, Any]:
        payload = _read_settings()
        if "github_token" in payload:
            payload.pop("github_token", None)
            try:
                _write_settings(payload)
            except OSError as exc:
                return {"ok": False, "error": f"无法清除令牌：{exc}"}
        return {"ok": True}

    def _persisted_token(self) -> str | None:
        return self._token_provider()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                **self._state,
                "latest": dict(self._state["latest"]) if self._state.get("latest") else None,
            }

    def has_token(self) -> bool:
        return bool(self._persisted_token())

    def check(self, include_prerelease: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"ok": False, "state": self._state["state"], "error": "已有更新任务正在执行"}
            self._state.update({
                "state": "checking",
                "message": f"正在检查更新（{SOURCE_LABELS.get(self.sources[0], self.sources[0])} 优先）…",
                "error": None,
                "progress": 0.0,
                "downloaded_bytes": 0,
                "total_bytes": 0,
                "download_speed_bps": 0.0,
                "download_stage": "",
                "latest": None,
                "history": [],
                "source": None,
                "include_prerelease": bool(include_prerelease),
            })
            thread = threading.Thread(
                target=self._check_worker, args=(bool(include_prerelease),),
                name="canhost-update-check", daemon=True,
            )
            self._thread = thread
        thread.start()
        return {"ok": True, "state": "checking"}

    def start_download(self, tag: str | None = None) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"ok": False, "state": self._state["state"], "error": "已有更新任务正在执行"}
            if self._state.get("installing"):
                return {"ok": False, "state": self._state["state"],
                        "error": "正在退出并安装更新，请等待应用重新启动"}
            latest = dict(self._state["latest"]) if self._state.get("latest") else None
            if not latest:
                return {"ok": False, "state": self._state["state"], "error": "请先检查更新"}
            if tag and tag != latest.get("tag_name"):
                return {"ok": False, "state": self._state["state"],
                        "error": f"没有版本 {tag} 的检查结果，请重新检查更新"}
            zip_asset = find_zip_asset(latest)
            self._state.update({
                "state": "downloading",
                "message": f"正在下载 {latest.get('tag_name')}…",
                "error": None,
                "progress": 0.0,
                "downloaded_bytes": 0,
                "total_bytes": int(zip_asset.get("size") or 0) if zip_asset else 0,
                "download_speed_bps": 0.0,
                "download_stage": "checksum",
                "downloaded_zip": "",
                "stage_dir": "",
                "install_error": None,
            })
            thread = threading.Thread(
                target=self._download_worker, args=(dict(latest),),
                name="canhost-update-download", daemon=True,
            )
            self._thread = thread
        thread.start()
        return {"ok": True, "state": "downloading"}

    def start_install(self, app_dir: Path) -> dict[str, Any]:
        """Exit the current process after handing replacement to PowerShell."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"ok": False, "state": self._state["state"], "error": "下载尚未完成"}
            if self._state.get("installing"):
                return {"ok": False, "state": self._state["state"],
                        "error": "应用正在退出并安装更新"}
            if self._state.get("state") != "ready":
                return {"ok": False, "state": self._state["state"],
                        "error": "没有已下载并校验的更新包"}
            if not install_ready():
                self._set(state="install_failed", message="源码运行不支持安装",
                          error="源码运行只能检查 GitHub Release；请使用 Windows 发布版执行更新安装。",
                          install_error="source-run install rejected")
                return {"ok": False, "state": "install_failed",
                        "error": "源码运行只支持检查更新，不能替换安装目录"}
            try:
                stage_dir = Path(str(self._state["stage_dir"])).resolve()
                work_dir = stage_dir.parent
            except (KeyError, OSError, TypeError, ValueError):
                return {"ok": False, "state": "install_failed",
                        "error": "已下载的更新目录不存在，请重新下载"}
            try:
                latest = self._state.get("latest") or {}
                launch_installer(app_dir=Path(app_dir).resolve(), stage_dir=stage_dir,
                                 work_dir=work_dir,
                                 expected_version=str(latest.get("tag_name") or ""),
                                 current_pid=os.getpid())
            except Exception as exc:
                self._set(state="install_failed", message="启动安装助手失败",
                          error=str(exc), install_error=str(exc))
                return {"ok": False, "state": "install_failed", "error": str(exc)}
            self._set(state="installing", message="应用即将退出并安装更新",
                      installing=True, install_error=None)
        return {"ok": True, "state": "installing"}

    def _set(self, **changes: Any) -> None:
        with self._lock:
            self._state.update(changes)

    def _headers(self, accept: str) -> dict[str, str]:
        """GitHub request headers, including the optional read-only token."""
        headers = {
            "User-Agent": "BITFSAE-CAN-Host-Updater/1.0",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = self._persisted_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def _cnb_headers(accept: str = "application/json") -> dict[str, str]:
        """CNB mirror headers: the channel and assets are readable anonymously."""
        return {"User-Agent": "BITFSAE-CAN-Host-Updater/1.0", "Accept": accept}

    def _source_headers(self, source: str, accept: str = "application/json") -> dict[str, str]:
        return self._headers(accept) if source == SOURCE_GITHUB else self._cnb_headers(accept)

    def _request(
        self,
        url: str,
        accept: str = "application/vnd.github+json",
        source: str = SOURCE_GITHUB,
    ) -> Any:
        headers = self._source_headers(source, accept)
        request = urllib.request.Request(url, headers=headers)
        return urllib.request.urlopen(request, timeout=self.timeout, context=SSL_CONTEXT)

    def _fetch_json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        request = urllib.request.Request(
            url, headers=headers if headers is not None else self._headers("application/json")
        )
        with urllib.request.urlopen(request, timeout=self.timeout, context=SSL_CONTEXT) as response:
            return json.loads(response.read().decode("utf-8"))

    def _github_releases(self) -> list[dict[str, Any]]:
        payload = self._fetch_json(
            f"https://api.github.com/repos/{self.repo}/releases?per_page=10",
            self._source_headers(SOURCE_GITHUB, "application/vnd.github+json"),
        )
        if not isinstance(payload, list):
            raise RuntimeError("GitHub Release 返回格式不正确")
        return [item for item in payload if isinstance(item, dict)]

    def _cnb_releases(self) -> list[dict[str, Any]]:
        """Read the CNB update channel: an anonymously readable JSON release list."""
        payload = self._fetch_json(cnb_channel_url(self.cnb_repo), self._source_headers(SOURCE_CNB))
        if not isinstance(payload, dict):
            raise RuntimeError("CNB 更新频道返回格式不正确")
        releases = payload.get("releases")
        if not isinstance(releases, list):
            raise RuntimeError("CNB 更新频道缺少 releases 列表")
        return [item for item in releases if isinstance(item, dict)]

    def _releases_from(self, source: str) -> list[dict[str, Any]]:
        if source == SOURCE_CNB:
            return self._cnb_releases()
        if source == SOURCE_GITHUB:
            return self._github_releases()
        raise ValueError(f"未知的更新源：{source}")

    @staticmethod
    def _select_release(releases: Iterable[dict[str, Any]], include_prerelease: bool) -> dict[str, Any] | None:
        candidates = (release for release in releases if not release.get("draft"))
        return next(
            (release for release in candidates if include_prerelease or not release.get("prerelease")),
            None,
        )

    @staticmethod
    def _history(releases: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """Recent releases for the update dialog's history list, newest first."""
        items = [release for release in releases if isinstance(release, dict) and not release.get("draft")]
        return [_history_summary(item) for item in items[:HISTORY_LIMIT]]

    @staticmethod
    def _http_error_message(exc: urllib.error.HTTPError, source: str = SOURCE_GITHUB) -> str:
        code = exc.code
        if source == SOURCE_CNB:
            if code == 404:
                return "CNB 镜像上还没有更新频道（HTTP 404），等待发布同步完成。"
            if code in (401, 403):
                return f"CNB 镜像拒绝访问（HTTP {code}），请稍后重试。"
            return f"CNB 镜像请求失败（HTTP {code}）。"
        if code in (401, 403):
            return (f"GitHub 拒绝访问（HTTP {code}）。仓库可能是私有仓库且未配置只读令牌；"
                    f"请在更新窗口“私有仓库访问令牌”中保存可下载 Release 的令牌。")
        if code == 404:
            return f"GitHub 上没有找到 Release（HTTP 404），请确认仓库与发布标签存在。"
        return f"GitHub 请求失败（HTTP {code}）。"

    @staticmethod
    def _is_certificate_error(exc: BaseException) -> bool:
        """Recognize certificate failures, including URLError-wrapped SSL errors."""
        current: BaseException | object | None = exc
        seen: set[int] = set()
        while isinstance(current, BaseException) and id(current) not in seen:
            seen.add(id(current))
            if (isinstance(current, ssl.SSLCertVerificationError)
                    or "CERTIFICATE_VERIFY_FAILED" in str(current)):
                return True
            if isinstance(current, urllib.error.URLError):
                current = current.reason
            else:
                current = current.__cause__ or current.__context__
        return False

    @staticmethod
    def _tls_error_message() -> str:
        """Friendly text for certificate failures on both update sources."""
        return ("HTTPS 证书校验失败，无法连接更新源。请升级到最新发布包后重试；"
                "若仍复现，检查系统时间或代理/防火墙")

    def _check_worker(self, include_prerelease: bool) -> None:
        thread = threading.current_thread()
        problems: list[str] = []
        try:
            for source in self.sources:
                label = SOURCE_LABELS.get(source, source)
                try:
                    releases = self._releases_from(source)
                except urllib.error.HTTPError as exc:
                    problems.append(f"{label}：{self._http_error_message(exc, source)}")
                    continue
                except (urllib.error.URLError, ssl.SSLError) as exc:
                    if self._is_certificate_error(exc):
                        problems.append(f"{label}：{self._tls_error_message()}")
                    else:
                        problems.append(f"{label}：{exc}")
                    continue
                except Exception as exc:
                    problems.append(f"{label}：{exc}")
                    continue
                selected = self._select_release(releases, include_prerelease)
                if selected is None:
                    problems.append(f"{label}：没有可用的正式发布版本，请稍后再试。")
                    continue
                tag = str(selected.get("tag_name") or "")
                summary = _release_summary(selected)
                try:
                    newer = release_is_newer(tag, self.current_version)
                except ValueError as exc:
                    problems.append(f"{label}：{exc}")
                    continue
                if newer:
                    remember_release_record(summary)
                    self._set(
                        state="update_available",
                        message=f"发现新版本 {tag}（{label}）",
                        error=None,
                        latest=summary,
                        history=self._history(releases),
                        source=source,
                        checked_at=time.time(),
                    )
                else:
                    self._set(
                        state="up_to_date",
                        message=f"当前已是 {self.current_version}，无需更新",
                        error=None,
                        latest=summary,
                        history=self._history(releases),
                        source=source,
                        checked_at=time.time(),
                    )
                return
            raise RuntimeError("；".join(problems) if problems else "没有可用的更新源")
        except Exception as exc:
            self._set(state="check_failed", message="检查更新失败",
                      error=str(exc), checked_at=time.time())
        finally:
            with self._lock:
                if self._thread is thread:
                    self._thread = None

    def _download_headers(self, url: str, accept: str = "application/octet-stream") -> dict[str, str]:
        """Token only for GitHub hosts; CNB mirror and signed URLs stay anonymous."""
        if is_github_url(url):
            return self._headers(accept)
        return self._cnb_headers(accept)

    def _download_payload(self, asset: dict[str, Any], target: Path, progress: bool = False) -> None:
        url = str(asset.get("url") or "")
        if not url:
            raise ValueError(f"发布资产 {asset.get('name')} 缺少下载地址")
        request = urllib.request.Request(url, headers=self._download_headers(url))
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        with urllib.request.urlopen(request, timeout=self.timeout, context=SSL_CONTEXT) as response:
            total = int(response.headers.get("Content-Length") or asset.get("size") or 0)
            done = 0
            started = time.monotonic()
            with partial.open("wb") as handle:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    if progress:
                        elapsed = max(time.monotonic() - started, 0.001)
                        self._set(
                            progress=min(1.0, done / total) if total else 0.0,
                            downloaded_bytes=done,
                            total_bytes=total,
                            download_speed_bps=done / elapsed,
                        )
        partial.replace(target)

    def _download_worker(self, latest: dict[str, Any]) -> None:
        thread = threading.current_thread()
        tag = str(latest.get("tag_name") or "")
        try:
            work_parent = Path(os.environ.get("TEMP") or tempfile.gettempdir()).resolve()
            cleanup_update_dirs(work_parent)
            zip_asset = find_zip_asset(latest)
            if not zip_asset:
                raise ValueError(f"Release {tag} 缺少 {APP_FOLDER_NAME}_{tag}.zip")
            zip_name = str(zip_asset.get("name"))
            checksum_asset = find_checksum_asset(latest, zip_name)
            if not checksum_asset:
                raise ValueError(f"Release {tag} 缺少 {zip_name}.sha256，无法校验更新包")
            work_dir = update_temp_dir(work_parent)
            zip_path = work_dir / zip_name
            checksum_path = work_dir / f"{zip_name}.sha256"
            self._set(download_stage="checksum")
            self._download_payload(checksum_asset, checksum_path)
            expected = read_sha256_digest(checksum_path)
            self._set(
                download_stage="archive",
                downloaded_bytes=0,
                total_bytes=int(zip_asset.get("size") or 0),
                download_speed_bps=0.0,
            )
            self._download_payload(zip_asset, zip_path, progress=True)
            self._set(download_stage="verifying")
            actual = hashlib.sha256(zip_path.read_bytes()).hexdigest().lower()
            if actual != expected:
                raise ValueError(f"更新包校验不一致：期望 {expected[:16]}…，实际 {actual[:16]}…")
            stage_dir = extract_update_archive(zip_path, work_dir)
            self._set(
                state="ready",
                message=f"{tag} 已下载并校验，可以重启安装",
                error=None,
                progress=1.0,
                downloaded_bytes=int(zip_path.stat().st_size),
                total_bytes=int(zip_path.stat().st_size),
                download_speed_bps=0.0,
                download_stage="ready",
                downloaded_zip=str(zip_path),
                stage_dir=str(stage_dir),
                install_error=None,
            )
        except urllib.error.HTTPError as exc:
            source = str(self._state.get("source") or SOURCE_GITHUB)
            self._set(state="download_failed", message="下载更新失败",
                      error=self._http_error_message(exc, source), progress=0.0)
        except (urllib.error.URLError, ssl.SSLError) as exc:
            error = self._tls_error_message() if self._is_certificate_error(exc) else str(exc)
            self._set(state="download_failed", message="下载更新失败",
                      error=error, progress=0.0)
        except Exception as exc:
            self._set(state="download_failed", message="下载更新失败",
                      error=str(exc), progress=0.0)
        finally:
            with self._lock:
                if self._thread is thread:
                    self._thread = None

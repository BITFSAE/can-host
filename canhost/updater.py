"""GitHub Release-based updater for the frozen Windows build."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable


DEFAULT_REPO = "BITFSAE/can-host"
APP_FOLDER_NAME = "BITFSAE_CAN_Host"
APP_EXE_NAME = f"{APP_FOLDER_NAME}.exe"
ASSET_PATTERN = re.compile(rf"^{APP_FOLDER_NAME}_v.+\\.zip$", re.IGNORECASE)
CHECKSUM_PATTERN = re.compile(rf"^{APP_FOLDER_NAME}_v.+\\.zip\\.sha256$", re.IGNORECASE)
SHA256_LINE = re.compile(r"(?m)^\s*([0-9a-fA-F]{64})")

SETTINGS_DIR_NAME = "BITFSAE"
SETTINGS_SUBDIR_NAME = "CAN Host"


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
    [int]$OldPid = 0
)
$ErrorActionPreference = "Stop"
$log = Join-Path $WorkDir "install-helper.log"
function Write-Log([string]$Message) {
    try { Add-Content -LiteralPath $log -Value $Message -Encoding UTF8 } catch {}
}
Write-Log "start staged=$StagedDir app=$AppDir"

$stagedExe = Join-Path $StagedDir $ExeName
if (-not (Test-Path -LiteralPath $stagedExe -PathType Leaf)) {
    Write-Log "staged exe missing"
    exit 21
}

if ($OldPid -gt 0) {
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
        $process = Get-Process -Id $OldPid -ErrorAction SilentlyContinue
        if (-not $process) { break }
        Start-Sleep -Milliseconds 250
    }
    if (Get-Process -Id $OldPid -ErrorAction SilentlyContinue) {
        Write-Log "old process still running"
        exit 22
    }
}

$backupPrefix = "$(Split-Path $AppDir -Leaf).old-"
$backupDir = Split-Path $AppDir -Parent
$backup = "$AppDir.old-$([DateTime]::UtcNow.ToString('yyyyMMddHHmmss'))"

$movedOld = $false
try {
    Rename-Item -LiteralPath $AppDir -NewName (Split-Path $backup -Leaf)
    $movedOld = $true
    Write-Log "old renamed $backup"
    Move-Item -LiteralPath $StagedDir -Destination $AppDir
    Write-Log "new moved"

    $newExe = Join-Path $AppDir $ExeName
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $newExe
    $psi.WorkingDirectory = $AppDir
    $psi.UseShellExecute = $true
    $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Normal
    $new = [System.Diagnostics.Process]::Start($psi)
    Write-Log "new process started pid=$($new.Id)"

    $deadline = (Get-Date).AddSeconds(8)
    $ok = $false
    while ((Get-Date) -lt $deadline) {
        $new.Refresh()
        if ($new.HasExited) {
            Write-Log "new process exited early code=$($new.ExitCode)"
            break
        }
        Start-Sleep -Milliseconds 250
        $ok = $true
    }
    if (-not $ok) {
        throw "new process exited early"
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
    if ($movedOld -and (Test-Path -LiteralPath $backup)) {
        try {
            if (Test-Path -LiteralPath $AppDir) { Remove-Item -LiteralPath $AppDir -Recurse -Force -ErrorAction Stop }
            Move-Item -LiteralPath $backup -Destination $AppDir
            Write-Log "old restored"
        } catch {
            Write-Log "restore failed: $_"
        }
    }
    exit 23
}
'''


def _powershell() -> Path:
    if os.name != "nt":
        raise RuntimeError("安装助手仅支持 Windows")
    return Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def launch_installer(
    app_dir: Path,
    stage_dir: Path,
    work_dir: Path,
    exe_name: str = APP_EXE_NAME,
    current_pid: int | None = None,
) -> Path:
    """Write a hidden PowerShell helper and start it detached from the app."""
    if not install_ready():
        raise RuntimeError("源码运行只支持检查更新，不能替换安装目录")
    if not stage_dir.is_absolute() or not work_dir.is_absolute():
        raise ValueError("安装目录必须是绝对路径")
    script_path = work_dir / "install-helper.ps1"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(INSTALLER_SCRIPT, encoding="utf-8")
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
        "-OldPid", str(current_pid or os.getpid()),
    ]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
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
    return {
        "tag_name": str(release.get("tag_name") or ""),
        "name": str(release.get("name") or ""),
        "html_url": str(release.get("html_url") or ""),
        "published_at": str(release.get("published_at") or ""),
        "prerelease": bool(release.get("prerelease", False)),
        "body": str(release.get("body") or "")[:12000],
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

class HostUpdater:
    """Background check/download state machine for GitHub releases."""

    def __init__(
        self,
        repo: str = DEFAULT_REPO,
        current_version: str = "0.0.0",
        token_provider: Callable[[], str | None] | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.repo = repo
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
            "latest": None,
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
                "message": "正在检查 GitHub Release…",
                "error": None,
                "progress": 0.0,
                "latest": None,
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
            self._state.update({
                "state": "downloading",
                "message": f"正在下载 {latest.get('tag_name')}…",
                "error": None,
                "progress": 0.0,
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
                launch_installer(app_dir=Path(app_dir).resolve(), stage_dir=stage_dir,
                                 work_dir=work_dir,
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
        headers = {
            "User-Agent": "BITFSAE-CAN-Host-Updater/1.0",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = self._persisted_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _request(self, url: str, accept: str = "application/vnd.github+json") -> Any:
        request = urllib.request.Request(url, headers=self._headers(accept))
        return urllib.request.urlopen(request, timeout=self.timeout)

    def _fetch_json(self, url: str) -> Any:
        with self._request(url) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _http_error_message(exc: urllib.error.HTTPError) -> str:
        code = exc.code
        if code in (401, 403):
            return (f"GitHub 拒绝访问（HTTP {code}）。仓库可能是私有仓库且未配置只读令牌；"
                    f"请在更新窗口“私有仓库访问令牌”中保存可下载 Release 的令牌。")
        if code == 404:
            return f"GitHub 上没有找到 Release（HTTP 404），请确认仓库与发布标签存在。"
        return f"GitHub 请求失败（HTTP {code}）。"

    def _check_worker(self, include_prerelease: bool) -> None:
        thread = threading.current_thread()
        try:
            releases = self._fetch_json(f"https://api.github.com/repos/{self.repo}/releases?per_page=10")
            if not isinstance(releases, list):
                raise RuntimeError("GitHub Release 返回格式不正确")
            candidates: Iterable[dict[str, Any]] = (
                release for release in releases if isinstance(release, dict) and not release.get("draft")
            )
            selected = next(
                (release for release in candidates if include_prerelease or not release.get("prerelease")),
                None,
            )
            if selected is None:
                raise RuntimeError("没有可用的正式发布版本，请稍后再试。")
            tag = str(selected.get("tag_name") or "")
            summary = _release_summary(selected)
            if release_is_newer(tag, self.current_version):
                self._set(
                    state="update_available",
                    message=f"发现新版本 {tag}",
                    error=None,
                    latest=summary,
                    checked_at=time.time(),
                )
            else:
                self._set(
                    state="up_to_date",
                    message=f"当前已是 {self.current_version}，无需更新",
                    error=None,
                    latest=summary,
                    checked_at=time.time(),
                )
        except urllib.error.HTTPError as exc:
            self._set(state="check_failed", message="检查更新失败",
                      error=self._http_error_message(exc), checked_at=time.time())
        except Exception as exc:
            self._set(state="check_failed", message="检查更新失败",
                      error=str(exc), checked_at=time.time())
        finally:
            with self._lock:
                if self._thread is thread:
                    self._thread = None

    def _download_payload(self, asset: dict[str, Any], target: Path, progress: bool = False) -> None:
        url = str(asset.get("url") or "")
        if not url:
            raise ValueError(f"发布资产 {asset.get('name')} 缺少下载地址")
        request = urllib.request.Request(url, headers=self._headers("application/octet-stream"))
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            with partial.open("wb") as handle:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    if progress and total:
                        self._set(progress=min(1.0, done / total))
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
            self._download_payload(checksum_asset, checksum_path)
            expected = read_sha256_digest(checksum_path)
            self._download_payload(zip_asset, zip_path, progress=True)
            actual = hashlib.sha256(zip_path.read_bytes()).hexdigest().lower()
            if actual != expected:
                raise ValueError(f"更新包校验不一致：期望 {expected[:16]}…，实际 {actual[:16]}…")
            stage_dir = extract_update_archive(zip_path, work_dir)
            self._set(
                state="ready",
                message=f"{tag} 已下载并校验，可以重启安装",
                error=None,
                progress=1.0,
                downloaded_zip=str(zip_path),
                stage_dir=str(stage_dir),
                install_error=None,
            )
        except urllib.error.HTTPError as exc:
            self._set(state="download_failed", message="下载更新失败",
                      error=self._http_error_message(exc), progress=0.0)
        except Exception as exc:
            self._set(state="download_failed", message="下载更新失败",
                      error=str(exc), progress=0.0)
        finally:
            with self._lock:
                if self._thread is thread:
                    self._thread = None

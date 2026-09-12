"""Tests for the GitHub Release updater and safe update archive handling."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from canhost.updater import (
    APP_EXE_NAME,
    APP_FOLDER_NAME,
    DEFAULT_CNB_REPO,
    DEFAULT_REPO,
    INSTALLER_SCRIPT,
    SOURCE_CNB,
    SOURCE_GITHUB,
    HostUpdater,
    cleanup_old_backups,
    cleanup_update_dirs,
    consume_update_result,
    cnb_channel_url,
    extract_update_archive,
    find_checksum_asset,
    find_zip_asset,
    is_github_url,
    read_sha256_digest,
    release_is_newer,
    startup_cleanup,
    version_key,
)


def _release(tag: str, zip_name: str | None = None, with_checksum: bool = True) -> dict:
    zip_name = zip_name or f"{APP_FOLDER_NAME}_{tag}.zip"
    assets = [
        {"id": 1, "name": zip_name, "size": 1024, "url": f"https://example/{zip_name}",
         "browser_download_url": f"https://example/{zip_name}"},
    ]
    if with_checksum:
        assets.append({"id": 2, "name": f"{zip_name}.sha256", "size": 80,
                       "url": f"https://example/{zip_name}.sha256",
                       "browser_download_url": f"https://example/{zip_name}.sha256"})
    return {"tag_name": tag, "name": tag, "html_url": f"https://example/{tag}",
            "published_at": "2026-08-27T00:00:00Z", "prerelease": False,
            "body": "release notes", "assets": assets}


def _write_update_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{APP_FOLDER_NAME}/{APP_EXE_NAME}", b"new executable")
        archive.writestr(f"{APP_FOLDER_NAME}/assets/config.json", b"{}")


def _make_update_zip(path: Path) -> bytes:
    _write_update_zip(path)
    return path.read_bytes()


class VersionAndAssetTest(unittest.TestCase):
    def test_version_sort_and_release_newer(self) -> None:
        self.assertGreater(version_key("v0.2.1"), version_key("v0.2.0"))
        self.assertGreater(version_key("v0.2.0-rc2"), version_key("v0.2.0-rc1"))
        self.assertGreater(version_key("V0.3.0"), version_key("v0.2.9"))
        self.assertTrue(release_is_newer("v0.3.0", "0.2.9"))
        self.assertFalse(release_is_newer("v0.2.9", "0.3.0"))

    def test_invalid_version_tag_raises(self) -> None:
        with self.assertRaises(ValueError):
            version_key("release-1")

    def test_find_zip_asset_prefers_exact_release_name(self) -> None:
        release = _release("v0.3.0", zip_name="unrelated.zip")
        self.assertIsNone(find_zip_asset(release))
        release["assets"].append({"id": 3, "name": f"{APP_FOLDER_NAME}_v0.3.0.zip",
                                  "size": 1, "url": "https://example/app.zip",
                                  "browser_download_url": "https://example/app.zip"})
        self.assertEqual(find_zip_asset(release)["name"], f"{APP_FOLDER_NAME}_v0.3.0.zip")

    def test_find_checksum_asset_matches_zip(self) -> None:
        release = _release("v0.3.0")
        zip_name = f"{APP_FOLDER_NAME}_v0.3.0.zip"
        asset = find_checksum_asset(release, zip_name)
        self.assertEqual(asset["name"], f"{zip_name}.sha256")
        self.assertIsNone(find_checksum_asset(release, f"{APP_FOLDER_NAME}_v0.2.0.zip"))

    def test_read_sha256_digest_accepts_common_formats(self) -> None:
        digest = hashlib.sha256(b"can-host-update").hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "check.sha256"
            path.write_text(f"{digest}  {APP_FOLDER_NAME}_v0.3.0.zip\n", encoding="utf-8")
            self.assertEqual(read_sha256_digest(path), digest)
            path.write_text(f"# sha256\n{digest}\n", encoding="utf-8")
            self.assertEqual(read_sha256_digest(path), digest)
            path.write_text("no checksum\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                read_sha256_digest(path)


class SafeArchiveTest(unittest.TestCase):
    def test_extract_accepts_one_folder_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            zip_path = root / "update.zip"
            _write_update_zip(zip_path)
            result = extract_update_archive(zip_path, root / "stage")
            self.assertTrue((result / APP_EXE_NAME).is_file())
            self.assertTrue((result / "assets" / "config.json").is_file())

    def test_extract_rejects_unsafe_paths_and_symlinks(self) -> None:
        cases = ["../escape.exe", "/absolute.exe", "C:/absolute.exe",
                 "BITFSAE_CAN_Host/../escape.exe", "BITFSAE_CAN_Host//x"]
        for name in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                zip_path = root / "update.zip"
                with zipfile.ZipFile(zip_path, "w") as archive:
                    archive.writestr(name, b"bad")
                    archive.writestr(f"{APP_FOLDER_NAME}/{APP_EXE_NAME}", b"exe")
                with self.assertRaises(ValueError):
                    extract_update_archive(zip_path, root / "stage")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            zip_path = root / "update.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                info = zipfile.ZipInfo(f"{APP_FOLDER_NAME}/link")
                info.external_attr = (0o120777 << 16)
                archive.writestr(info, "target")
                archive.writestr(f"{APP_FOLDER_NAME}/{APP_EXE_NAME}", b"exe")
            with self.assertRaises(ValueError):
                extract_update_archive(zip_path, root / "stage")


class BackupCleanupTest(unittest.TestCase):
    def test_cleanup_keeps_active_update_handoff_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            active = parent / "canhost-update-active"
            stale = parent / "canhost-update-stale"
            active.mkdir()
            stale.mkdir()
            (active / "update-health.json").write_text("{}", encoding="utf-8")
            (stale / "payload.bin").write_bytes(b"x")
            self.assertEqual(cleanup_update_dirs(parent, keep={active}), 1)
            self.assertTrue(active.is_dir())
            self.assertFalse(stale.exists())

    def test_cleanup_deletes_old_backups_keeps_fresh_and_unrelated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            now = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc).timestamp()
            stale = parent / f"{APP_FOLDER_NAME}.old-20260901120000"
            fresh = parent / f"{APP_FOLDER_NAME}.old-20260904115930"
            malformed = parent / f"{APP_FOLDER_NAME}.old-notadate"
            current = parent / APP_FOLDER_NAME
            for item in (stale, fresh, malformed, current):
                item.mkdir()
                (item / "payload.bin").write_bytes(b"x")
            removed = cleanup_old_backups(parent, now=now)
            self.assertEqual(removed, 1)
            self.assertFalse(stale.exists())
            # 回退窗口内的备份、正在使用的安装目录和无法识别的目录都必须保留。
            self.assertTrue(fresh.exists())
            self.assertTrue(malformed.exists())
            self.assertTrue(current.exists())

    def test_startup_cleanup_counts_backups_and_temp_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app_dir = Path(directory) / APP_FOLDER_NAME
            app_dir.mkdir()
            now = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc).timestamp()
            stale = app_dir.parent / f"{APP_FOLDER_NAME}.old-20260901120000"
            stale.mkdir()
            with patch("canhost.updater.cleanup_update_dirs", return_value=2) as temp_cleanup:
                result = startup_cleanup(app_dir, now=now)
            self.assertEqual(result, {"old_backups": 1, "temp_dirs": 2})
            temp_cleanup.assert_called_once()
            self.assertFalse(stale.exists())


class InstallerPackagingTest(unittest.TestCase):
    """Anchor the Inno Setup installer to the updater's directory-swap assumptions."""

    def test_install_helper_preserves_inno_uninstaller(self) -> None:
        # The update ZIP has no unins000.*; without a copy-back from the old
        # backup, a setup.exe install would lose its "Apps & Features" entry
        # after the first in-app update.
        self.assertIn("unins000.exe", INSTALLER_SCRIPT)
        self.assertIn("unins000.dat", INSTALLER_SCRIPT)
        self.assertIn("Copy-Item", INSTALLER_SCRIPT)

    def test_install_helper_script_is_pure_ascii(self) -> None:
        # install-helper.ps1 由 Windows PowerShell 5.1 按 -File 执行，且以无
        # BOM UTF-8 落盘；任何非 ASCII 字符在 ANSI 误读下可能变成弯引号并
        # 提前终止字符串，直接破坏安装助手解析。
        self.assertTrue(INSTALLER_SCRIPT.isascii())

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell parser is Windows-only")
    def test_install_helper_parses_in_windows_powershell(self) -> None:
        encoded = base64.b64encode(INSTALLER_SCRIPT.encode("utf-8")).decode("ascii")
        command = (
            "$source=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:CANHOST_SCRIPT_B64));"
            "[ScriptBlock]::Create($source) | Out-Null"
        )
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / (
            r"System32\WindowsPowerShell\v1.0\powershell.exe"
        )
        environment = os.environ.copy()
        environment["CANHOST_SCRIPT_B64"] = encoded
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_install_helper_requires_ui_health_and_restarts_rollback(self) -> None:
        self.assertIn("--update-health-file", INSTALLER_SCRIPT)
        self.assertIn("frontend healthy", INSTALLER_SCRIPT)
        self.assertIn("health pid mismatch", INSTALLER_SCRIPT)
        self.assertIn("health version mismatch", INSTALLER_SCRIPT)
        self.assertIn("Start-App $AppDir $false", INSTALLER_SCRIPT)
        self.assertIn("old process restarted", INSTALLER_SCRIPT)
        self.assertIn("forcing stop", INSTALLER_SCRIPT)
        self.assertIn("PresentationFramework", INSTALLER_SCRIPT)

    def test_failed_helper_result_is_consumed_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "last-update-result.json"
            result_path.write_text(json.dumps({
                "ok": False,
                "code": "health_timeout",
                "log_path": r"C:\logs\update.log",
            }), encoding="utf-8")
            with patch("canhost.updater.update_result_path", return_value=result_path):
                result = consume_update_result()
                self.assertEqual(result["code"], "health_timeout")
                self.assertIn("恢复", result["message"])
                self.assertEqual(result["log_path"], r"C:\logs\update.log")
                self.assertIsNone(consume_update_result())
            self.assertFalse(result_path.exists())

    def test_inno_setup_uses_updater_folder_layout(self) -> None:
        iss = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "canhost.iss"
        text = iss.read_text(encoding="utf-8")
        # 整目录替换要求安装目录名与 APP_FOLDER_NAME 一致，且每用户可写（无需管理员）。
        self.assertIn(r"DefaultDirName={localappdata}\Programs\BITFSAE_CAN_Host", text)
        self.assertIn("PrivilegesRequired=lowest", text)
        self.assertIn(APP_EXE_NAME, text)
        # 卸载必须连带清理安装目录上一级的旧版本备份目录。
        self.assertIn("BITFSAE_CAN_Host.old-*", text)


class HostUpdaterTest(unittest.TestCase):
    def test_check_worker_reports_update_or_up_to_date(self) -> None:
        updater = HostUpdater(current_version="0.2.0")
        with patch.object(updater, "_fetch_json", return_value=[_release("v0.3.0")]):
            updater._check_worker(False)
        status = updater.status()
        self.assertEqual(status["state"], "update_available")
        self.assertEqual(status["latest"]["tag_name"], "v0.3.0")

        updater = HostUpdater(current_version="0.3.0")
        with patch.object(updater, "_fetch_json", return_value=[_release("v0.3.0")]):
            updater._check_worker(False)
        self.assertEqual(updater.status()["state"], "up_to_date")

    def test_check_worker_filters_private_access_as_token_hint(self) -> None:
        updater = HostUpdater(current_version="0.2.0")
        error = urllib.error.HTTPError("https://api.github.com/", 403, "Forbidden", None, None)
        with patch.object(updater, "_fetch_json", side_effect=error):
            updater._check_worker(False)
        status = updater.status()
        self.assertEqual(status["state"], "check_failed")
        self.assertIn("私有仓库", status["error"])

    def test_download_worker_verifies_checksum_and_reaches_ready(self) -> None:
        updater = HostUpdater(current_version="0.2.0")
        summary = _release("v0.3.0")
        updater._state["latest"] = summary
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "update.zip"
            zip_bytes = _make_update_zip(archive_path)
        checksum = hashlib.sha256(zip_bytes).hexdigest()
        calls = []
        expected_urls = []

        def fake_download(asset, target, progress=False):
            calls.append(str(asset["name"]))
            expected_urls.append(str(asset["url"]))
            target.parent.mkdir(parents=True, exist_ok=True)
            if str(asset["name"]).endswith(".sha256"):
                target.write_text(f"{checksum}  {asset['name']}\n", encoding="utf-8")
            else:
                target.write_bytes(zip_bytes)

        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            with patch("canhost.updater.cleanup_update_dirs", return_value=0), \
                 patch("canhost.updater.update_temp_dir", return_value=work / "canhost-update-test"), \
                 patch.object(updater, "_download_payload", side_effect=fake_download):
                updater._download_worker(summary)
        status = updater.status()
        self.assertEqual(status["state"], "ready")
        self.assertTrue(status["stage_dir"])
        self.assertEqual(status["progress"], 1.0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(expected_urls), 2)

    def test_http_error_message_mentions_private_token_only_on_access_denied(self) -> None:
        updater = HostUpdater()
        denied = urllib.error.HTTPError("https://api.github.com/", 403, "Forbidden", None, None)
        self.assertIn("私有仓库", updater._http_error_message(denied))
        missing = urllib.error.HTTPError("https://api.github.com/", 404, "Not Found", None, None)
        self.assertNotIn("私有仓库", updater._http_error_message(missing))
        self.assertIn("404", updater._http_error_message(missing))

    def test_download_worker_rejects_mismatched_checksum(self) -> None:
        updater = HostUpdater(current_version="0.2.0")
        summary = _release("v0.3.0")
        updater._state["latest"] = summary
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "update.zip"
            zip_bytes = _make_update_zip(archive_path)
        wrong_checksum = hashlib.sha256(b"not the zip").hexdigest()
        calls = []

        def fake_download(asset, target, progress=False):
            calls.append(str(asset["name"]))
            target.parent.mkdir(parents=True, exist_ok=True)
            if str(asset["name"]).endswith(".sha256"):
                target.write_text(f"{wrong_checksum}  {asset['name']}\n", encoding="utf-8")
            else:
                target.write_bytes(zip_bytes)

        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            with patch("canhost.updater.cleanup_update_dirs", return_value=0), \
                 patch("canhost.updater.update_temp_dir", return_value=work / "canhost-update-test"), \
                 patch.object(updater, "_download_payload", side_effect=fake_download):
                updater._download_worker(summary)
        self.assertEqual(updater.status()["state"], "download_failed")
        self.assertIn("校验不一致", updater.status()["error"])

    def test_token_provider_is_used_for_requests(self) -> None:
        calls = []

        def provider():
            calls.append(1)
            return "gho_read_only"

        updater = HostUpdater(token_provider=provider)
        self.assertEqual(updater._persisted_token(), "gho_read_only")
        self.assertTrue(updater.has_token())
        self.assertEqual(len(calls), 2)

    def test_token_persistence_is_scoped_to_test_settings(self) -> None:
        updater = HostUpdater()
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            with patch("canhost.updater.settings_path", return_value=settings):
                self.assertTrue(updater.set_token("gho_read_only")["ok"])
                payload = json.loads(settings.read_text(encoding="utf-8"))
                self.assertEqual(payload["github_token"], "gho_read_only")
                self.assertTrue(updater.clear_token()["ok"])
                self.assertNotIn("github_token", json.loads(settings.read_text(encoding="utf-8")))

    def test_source_run_rejects_install(self) -> None:
        updater = HostUpdater(current_version="0.3.0")
        updater._state.update({"state": "ready", "stage_dir": str(Path.cwd())})
        with patch("canhost.updater.install_ready", return_value=False):
            result = updater.start_install(Path.cwd())
        self.assertFalse(result["ok"])
        self.assertEqual(updater.status()["state"], "install_failed")
        self.assertIn("源码运行", result["error"])

    def test_default_repo_is_github_org_repo(self) -> None:
        self.assertEqual(DEFAULT_REPO, "BITFSAE/can-host")
        self.assertEqual(DEFAULT_CNB_REPO, "totok22/can-host")


def _cnb_channel(tag: str = "v0.3.0", prerelease: bool = False) -> dict:
    zip_name = f"{APP_FOLDER_NAME}_{tag}.zip"
    base = f"https://cnb.cool/{DEFAULT_CNB_REPO}/-/releases/download/{tag}"
    return {
        "schema": 1,
        "repo": DEFAULT_CNB_REPO,
        "updated_at": "2026-09-12T00:00:00Z",
        "releases": [{
            "tag_name": tag,
            "name": tag,
            "prerelease": prerelease,
            "draft": False,
            "published_at": "2026-09-04T10:01:22Z",
            "body": "release notes",
            "assets": [
                {"id": "1", "name": zip_name, "size": 1024,
                 "url": f"{base}/{zip_name}", "browser_download_url": f"{base}/{zip_name}"},
                {"id": "2", "name": f"{zip_name}.sha256", "size": 95,
                 "url": f"{base}/{zip_name}.sha256",
                 "browser_download_url": f"{base}/{zip_name}.sha256"},
            ],
        }],
    }


class CnbMirrorSourceTest(unittest.TestCase):
    """国内镜像优先、GitHub 回退的检查与下载路径。"""

    def test_channel_url_is_anonymous_cnb_raw(self) -> None:
        self.assertEqual(
            cnb_channel_url(DEFAULT_CNB_REPO),
            f"https://cnb.cool/{DEFAULT_CNB_REPO}/-/git/raw/cnb-update/latest.json",
        )

    def test_github_url_detection_is_host_scoped(self) -> None:
        self.assertTrue(is_github_url("https://api.github.com/repos/a/b/releases"))
        self.assertTrue(is_github_url("https://objects.githubusercontent.com/x"))
        self.assertFalse(is_github_url("https://cnb.cool/totok22/can-host/-/releases/download/v1/a.zip"))
        self.assertFalse(is_github_url("https://asset.cnb.cool/assets/x"))

    def test_check_uses_cnb_channel_and_never_calls_github(self) -> None:
        updater = HostUpdater(current_version="0.2.0")
        urls: list[str] = []

        def fake_fetch(url, headers=None):
            urls.append(url)
            if "cnb.cool" not in url:
                raise AssertionError(f"CNB 可用时不应访问 {url}")
            self.assertNotIn("Authorization", headers or {})
            return _cnb_channel("v0.3.0")

        with patch.object(updater, "_fetch_json", side_effect=fake_fetch):
            updater._check_worker(False)
        status = updater.status()
        self.assertEqual(status["state"], "update_available")
        self.assertEqual(status["source"], SOURCE_CNB)
        self.assertEqual(status["latest"]["tag_name"], "v0.3.0")
        self.assertTrue(status["latest"]["assets"][0]["url"].startswith("https://cnb.cool/"))
        zip_asset = find_zip_asset(status["latest"])
        self.assertIsNotNone(zip_asset)
        self.assertIn(DEFAULT_CNB_REPO, zip_asset["url"])
        self.assertEqual(len(urls), 1)

    def test_check_falls_back_to_github_when_mirror_unreachable(self) -> None:
        updater = HostUpdater(current_version="0.2.0")

        def fake_fetch(url, headers=None):
            if "cnb.cool" in url:
                raise urllib.error.URLError("timed out")
            return [_release("v0.3.0")]

        with patch.object(updater, "_fetch_json", side_effect=fake_fetch):
            updater._check_worker(False)
        status = updater.status()
        self.assertEqual(status["state"], "update_available")
        self.assertEqual(status["source"], SOURCE_GITHUB)

    def test_check_reports_both_sources_when_all_fail(self) -> None:
        updater = HostUpdater(current_version="0.2.0")

        def fake_fetch(url, headers=None):
            raise urllib.error.URLError("网络不可达")

        with patch.object(updater, "_fetch_json", side_effect=fake_fetch):
            updater._check_worker(False)
        status = updater.status()
        self.assertEqual(status["state"], "check_failed")
        self.assertIn("CNB 镜像", status["error"])
        self.assertIn("GitHub", status["error"])

    def test_saved_github_token_never_goes_to_cnb(self) -> None:
        updater = HostUpdater(token_provider=lambda: "gho_read_only")
        self.assertNotIn("Authorization", updater._cnb_headers())
        cnb_url = f"https://cnb.cool/{DEFAULT_CNB_REPO}/-/releases/download/v0.3.0/a.zip"
        self.assertNotIn("Authorization", updater._download_headers(cnb_url))
        github_url = "https://api.github.com/repos/BITFSAE/can-host/releases/assets/1"
        self.assertIn("Authorization", updater._download_headers(github_url))

    def test_download_uses_cnb_asset_urls_from_channel(self) -> None:
        updater = HostUpdater(current_version="0.2.0")
        with patch.object(updater, "_fetch_json", return_value=_cnb_channel("v0.3.0")):
            updater._check_worker(False)
        summary = updater.status()["latest"]
        downloaded: list[str] = []

        def fake_download(asset, target, progress=False):
            downloaded.append(str(asset["url"]))
            target.parent.mkdir(parents=True, exist_ok=True)
            if str(asset["name"]).endswith(".sha256"):
                target.write_text(f"{hashlib.sha256(b'').hexdigest()}  {asset['name']}\n", encoding="utf-8")
            else:
                target.write_bytes(b"")

        def fake_extract(zip_path, work_dir, exe_name=APP_EXE_NAME):
            return work_dir / APP_FOLDER_NAME

        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            with patch("canhost.updater.cleanup_update_dirs", return_value=0), \
                 patch("canhost.updater.update_temp_dir", return_value=work / "canhost-update-test"), \
                 patch("canhost.updater.extract_update_archive", side_effect=fake_extract), \
                 patch.object(updater, "_download_payload", side_effect=fake_download):
                updater._download_worker(summary)
        self.assertEqual(updater.status()["state"], "ready")
        self.assertEqual(len(downloaded), 2)
        for url in downloaded:
            self.assertTrue(url.startswith("https://cnb.cool/"), url)


if __name__ == "__main__":
    unittest.main()

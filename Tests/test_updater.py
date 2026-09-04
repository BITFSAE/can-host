"""Tests for the GitHub Release updater and safe update archive handling."""

from __future__ import annotations

import hashlib
import json
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
    DEFAULT_REPO,
    INSTALLER_SCRIPT,
    HostUpdater,
    cleanup_old_backups,
    extract_update_archive,
    find_checksum_asset,
    find_zip_asset,
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


if __name__ == "__main__":
    unittest.main()

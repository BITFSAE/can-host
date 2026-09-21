"""Tests for the GitHub Release updater and safe update archive handling."""

from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import json
import os
import ssl
import subprocess
import sys
import tempfile
import threading
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
    MACOS_INSTALLER_SCRIPT,
    MAC_APP_BUNDLE_NAME,
    MAC_APP_EXE_RELATIVE,
    SOURCE_CNB,
    SOURCE_GITHUB,
    HostUpdater,
    cleanup_old_backups,
    cleanup_update_dirs,
    consume_update_result,
    cnb_channel_url,
    extract_update_archive,
    find_checksum_asset,
    find_update_asset,
    find_zip_asset,
    is_github_url,
    launch_installer,
    release_record_for,
    read_sha256_digest,
    remember_release_record,
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
            "body": "# 标题\n\n## 本次更新\n\n- 修复弹窗说明\n\n## 下载\n\n- `x.zip`\n",
            "assets": assets}


def _write_update_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{APP_FOLDER_NAME}/{APP_EXE_NAME}", b"new executable")
        archive.writestr(f"{APP_FOLDER_NAME}/assets/config.json", b"{}")


def _make_update_zip(path: Path) -> bytes:
    _write_update_zip(path)
    return path.read_bytes()


def _write_macos_update_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        executable = f"{MAC_APP_BUNDLE_NAME}/{MAC_APP_EXE_RELATIVE.as_posix()}"
        info = zipfile.ZipInfo(executable)
        info.external_attr = 0o100755 << 16
        archive.writestr(info, b"new mac executable")
        archive.writestr(f"{MAC_APP_BUNDLE_NAME}/Contents/Info.plist", b"plist")
        archive.writestr(f"__MACOSX/{MAC_APP_BUNDLE_NAME}/Contents/._Info.plist", b"meta")


class IsolatedSettingsTestCase(unittest.TestCase):
    """把更新器设置重定向到临时目录，任何用例都不许碰真实用户设置。

    ``_check_worker`` 成功时会经 ``remember_release_record`` 落盘“最近一次
    检查到的 Release”；没有重定向的用例会把假发布写进 ``~/.config/can-host``。
    """

    def setUp(self) -> None:
        super().setUp()
        self._settings_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._settings_dir.cleanup)
        root = Path(self._settings_dir.name)
        for target, name in (("settings_path", "settings.json"),
                             ("installed_version_path", "installed-version.json")):
            patcher = patch(f"canhost.updater.{target}", return_value=root / name)
            patcher.start()
            self.addCleanup(patcher.stop)


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

    def test_find_macos_update_asset_ignores_dmg_and_windows_zip(self) -> None:
        release = _release("v0.3.0")
        mac_name = f"{APP_FOLDER_NAME}_macOS_arm64_v0.3.0-update.zip"
        release["assets"].extend([
            {"name": f"{APP_FOLDER_NAME}_macOS_arm64_v0.3.0.dmg", "url": "https://example/app.dmg"},
            {"name": mac_name, "url": "https://example/mac-update.zip"},
            {"name": f"{mac_name}.sha256", "url": "https://example/mac-update.zip.sha256"},
        ])
        self.assertEqual(find_update_asset(release, "macos")["name"], mac_name)
        self.assertEqual(find_update_asset(release, "windows")["name"],
                         f"{APP_FOLDER_NAME}_v0.3.0.zip")

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

    def test_extract_macos_app_preserves_expected_root_and_metadata(self) -> None:
        from canhost import updater

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            zip_path = root / "mac-update.zip"
            _write_macos_update_zip(zip_path)
            real_is_file = Path.is_file
            with patch.object(
                updater.Path,
                "is_file",
                lambda candidate: False
                if candidate == Path("/usr/bin/ditto")
                else real_is_file(candidate),
            ):
                result = extract_update_archive(zip_path, root / "stage", platform="macos")
            executable = result / MAC_APP_EXE_RELATIVE
            self.assertEqual(result.name, MAC_APP_BUNDLE_NAME)
            self.assertTrue(executable.is_file())
            self.assertTrue(os.access(executable, os.X_OK))

    def test_extract_macos_rejects_symlink_outside_app(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            zip_path = root / "bad-mac-update.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                executable = f"{MAC_APP_BUNDLE_NAME}/{MAC_APP_EXE_RELATIVE.as_posix()}"
                archive.writestr(executable, b"exe")
                link = zipfile.ZipInfo(f"{MAC_APP_BUNDLE_NAME}/Contents/escape")
                link.external_attr = 0o120777 << 16
                archive.writestr(link, "../../../outside")
            with self.assertRaisesRegex(ValueError, "越出应用目录"):
                extract_update_archive(zip_path, root / "stage", platform="macos")

    @unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/ditto").is_file(),
                         "ditto archive test is macOS-only")
    def test_extracts_archive_created_by_release_ditto_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / MAC_APP_BUNDLE_NAME
            executable = app / MAC_APP_EXE_RELATIVE
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"signed app payload")
            executable.chmod(0o755)
            archive = root / "release-update.zip"
            subprocess.run(
                ["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
                 str(app), str(archive)],
                check=True,
            )
            staged = extract_update_archive(archive, root / "stage", platform="macos")
            self.assertEqual((staged / MAC_APP_EXE_RELATIVE).read_bytes(), b"signed app payload")


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

    def test_startup_cleanup_matches_macos_bundle_backup_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            app_dir = parent / MAC_APP_BUNDLE_NAME
            app_dir.mkdir()
            stale = parent / f"{MAC_APP_BUNDLE_NAME}.old-20260901120000"
            stale.mkdir()
            now = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc).timestamp()
            with patch("canhost.updater.cleanup_update_dirs", return_value=0):
                result = startup_cleanup(app_dir, now=now)
            self.assertEqual(result["old_backups"], 1)
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

    def test_install_helper_retries_a_busy_install_directory(self) -> None:
        self.assertIn("Rename-DirectoryWithRetry", INSTALLER_SCRIPT)
        self.assertIn('Write-Log "old rename blocked; retrying"', INSTALLER_SCRIPT)

    def test_macos_helper_swaps_app_and_requires_matching_health(self) -> None:
        self.assertIn('mv "$APP_DIR" "$BACKUP_DIR"', MACOS_INSTALLER_SCRIPT)
        self.assertIn('mv "$STAGED_DIR" "$APP_DIR"', MACOS_INSTALLER_SCRIPT)
        self.assertIn(r'\"pid\": $NEW_PID', MACOS_INSTALLER_SCRIPT)
        self.assertIn(r'\"version\": \"$EXPECTED_BASE\"', MACOS_INSTALLER_SCRIPT)
        self.assertIn('rollback "health_timeout"', MACOS_INSTALLER_SCRIPT)
        self.assertIn('open -n "$APP_DIR"', MACOS_INSTALLER_SCRIPT)

    def test_frozen_macos_install_is_supported_when_app_parent_is_writable(self) -> None:
        from canhost import updater

        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / MAC_APP_BUNDLE_NAME
            with patch.object(updater.sys, "frozen", True, create=True), \
                 patch.object(updater.sys, "platform", "darwin"), \
                 patch.object(updater, "installed_app_dir", return_value=app), \
                 patch.object(updater.os, "access", return_value=True):
                self.assertTrue(updater.install_ready())
            with patch.object(updater.sys, "frozen", True, create=True), \
                 patch.object(updater.sys, "platform", "darwin"), \
                 patch.object(updater, "installed_app_dir", return_value=app), \
                 patch.object(updater.os, "access", return_value=False):
                self.assertFalse(updater.install_ready())

    def test_macos_launcher_starts_detached_shell_helper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app_dir = root / "Applications" / MAC_APP_BUNDLE_NAME
            stage_dir = root / "downloads" / MAC_APP_BUNDLE_NAME
            executable = stage_dir / MAC_APP_EXE_RELATIVE
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"staged executable")
            work_dir = root / "updates" / "canhost-update-test"
            log_dir = root / "logs"
            result_path = root / "settings" / "last-update-result.json"

            with patch("canhost.updater.install_ready", return_value=True), \
                 patch("canhost.updater.update_platform", return_value="macos"), \
                 patch("canhost.updater.update_log_dir", return_value=log_dir), \
                 patch("canhost.updater.update_result_path", return_value=result_path), \
                 patch("canhost.updater.subprocess.Popen") as popen:
                helper = launch_installer(
                    app_dir=app_dir.resolve(),
                    stage_dir=stage_dir.resolve(),
                    work_dir=work_dir.resolve(),
                    expected_version="v0.9.12",
                    current_pid=1234,
                )

            self.assertEqual(helper.name, "install-helper.sh")
            if os.name != "nt":
                self.assertTrue(helper.stat().st_mode & 0o100)
            command = popen.call_args.args[0]
            self.assertEqual(Path(command[0]), helper)
            self.assertEqual(command[-1], "1234")
            self.assertTrue(popen.call_args.kwargs["start_new_session"])

    @unittest.skipUnless(sys.platform == "darwin", "macOS helper integration is macOS-only")
    def test_macos_helper_replaces_app_after_health_signal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "Applications" / MAC_APP_BUNDLE_NAME
            staged = root / "download" / MAC_APP_BUNDLE_NAME
            work = root / "canhost-update-helper"
            log = root / "logs" / "update.log"
            result = root / "settings" / "result.json"
            (app / "Contents").mkdir(parents=True)
            (app / "Contents" / "old-marker").write_text("old", encoding="utf-8")
            executable = staged / MAC_APP_EXE_RELATIVE
            executable.parent.mkdir(parents=True)
            executable.write_text(
                """#!/bin/sh
health="$2"
printf '{"pid": %s, "version": "0.9.12"}' "$$" > "$health"
sleep 1
""",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            (staged / "Contents" / "new-marker").write_text("new", encoding="utf-8")
            work.mkdir()
            helper = work / "install-helper.sh"
            helper.write_text(MACOS_INSTALLER_SCRIPT, encoding="utf-8")
            helper.chmod(0o700)

            completed = subprocess.run(
                [str(helper), str(app), str(staged), str(work), "v0.9.12",
                 str(log), str(work / "health.json"), str(result), "999999"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            self.assertTrue((app / "Contents" / "new-marker").is_file())
            self.assertFalse((app / "Contents" / "old-marker").exists())
            self.assertFalse(work.exists())
            self.assertFalse(result.exists())
            self.assertIn("frontend healthy", log.read_text(encoding="utf-8"))

    def test_launcher_runs_helper_outside_install_and_work_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app_dir = root / "installed" / APP_FOLDER_NAME
            stage_dir = root / "downloads" / APP_FOLDER_NAME
            work_dir = root / "updates" / "canhost-update-test"
            stage_dir.mkdir(parents=True)
            (stage_dir / APP_EXE_NAME).write_bytes(b"staged executable")
            log_dir = root / "logs"
            result_path = root / "settings" / "last-update-result.json"

            with patch("canhost.updater.install_ready", return_value=True), \
                    patch("canhost.updater._powershell", return_value=Path("powershell.exe")), \
                    patch("canhost.updater.update_log_dir", return_value=log_dir), \
                    patch("canhost.updater.update_result_path", return_value=result_path), \
                    patch("canhost.updater.subprocess.Popen") as popen:
                launch_installer(
                    app_dir=app_dir.resolve(),
                    stage_dir=stage_dir.resolve(),
                    work_dir=work_dir.resolve(),
                    expected_version="v0.9.3",
                    current_pid=1234,
                )

            helper_cwd = Path(popen.call_args.kwargs["cwd"])
            self.assertEqual(helper_cwd, work_dir.parent.resolve())
            self.assertNotEqual(helper_cwd, app_dir.resolve())
            self.assertNotEqual(helper_cwd, work_dir.resolve())

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


class HostUpdaterTest(IsolatedSettingsTestCase):
    def test_automatic_and_manual_check_cannot_start_overlapping_workers(self) -> None:
        updater = HostUpdater(current_version="0.2.0", sources=(SOURCE_CNB,))
        start_entered = threading.Event()
        allow_start = threading.Event()
        fetch_entered = threading.Event()
        allow_fetch = threading.Event()
        second_entered = threading.Event()
        results: dict[str, dict] = {}
        real_start = threading.Thread.start

        def delayed_start(thread):
            if thread.name == "canhost-update-check" and not start_entered.is_set():
                start_entered.set()
                self.assertTrue(allow_start.wait(2))
            return real_start(thread)

        def delayed_fetch(source):
            fetch_entered.set()
            self.assertTrue(allow_fetch.wait(2))
            return [_release("v0.3.0")]

        def invoke(name):
            if name == "manual":
                second_entered.set()
            results[name] = updater.check(False)

        with patch.object(threading.Thread, "start", autospec=True, side_effect=delayed_start), \
             patch.object(updater, "_releases_from", side_effect=delayed_fetch):
            automatic = threading.Thread(target=invoke, args=("automatic",))
            manual = threading.Thread(target=invoke, args=("manual",))
            real_start(automatic)
            self.assertTrue(start_entered.wait(2))
            real_start(manual)
            self.assertTrue(second_entered.wait(2))
            allow_start.set()
            self.assertTrue(fetch_entered.wait(2))
            automatic.join(2)
            manual.join(2)
            worker = updater._thread
            allow_fetch.set()
            if worker is not None:
                worker.join(2)

        self.assertTrue(results["automatic"]["ok"])
        self.assertFalse(results["manual"]["ok"])
        self.assertIn("已有更新任务", results["manual"]["error"])

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
            with patch("canhost.updater.cleanup_update_dirs", return_value=0) as cleanup, \
                 patch("canhost.updater.update_temp_dir", return_value=work / "canhost-update-test"), \
                 patch.object(updater, "_download_payload", side_effect=fake_download):
                updater._download_worker(summary)
        status = updater.status()
        self.assertEqual(status["state"], "ready")
        self.assertTrue(status["stage_dir"])
        self.assertEqual(status["progress"], 1.0)
        self.assertEqual(status["download_stage"], "ready")
        self.assertEqual(status["downloaded_bytes"], status["total_bytes"])
        self.assertEqual(calls, [str(summary["assets"][0]["name"]),
                                 str(summary["assets"][1]["name"])])
        self.assertEqual(len(expected_urls), 2)
        cleanup.assert_not_called()

    def test_download_payload_reports_bytes_progress_and_speed(self) -> None:
        updater = HostUpdater()

        class Response(BytesIO):
            # Some mirrors omit Content-Length; the release asset size must
            # still keep the UI percentage meaningful.
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "update.zip"
            with patch.object(updater, "_open_url", return_value=Response(b"abcdef")):
                updater._download_payload(
                    {"url": "https://example/update.zip", "size": 6}, target, progress=True
                )
            self.assertEqual(target.read_bytes(), b"abcdef")
        status = updater.status()
        self.assertEqual(status["downloaded_bytes"], 6)
        self.assertEqual(status["total_bytes"], 6)
        self.assertEqual(status["progress"], 1.0)
        self.assertEqual(status["download_stage"], "archive")
        self.assertGreater(status["download_speed_bps"], 0)

    def test_http_error_message_mentions_private_token_only_on_access_denied(self) -> None:
        updater = HostUpdater()
        denied = urllib.error.HTTPError("https://api.github.com/", 403, "Forbidden", None, None)
        self.assertIn("私有仓库", updater._http_error_message(denied))
        missing = urllib.error.HTTPError("https://api.github.com/", 404, "Not Found", None, None)
        self.assertNotIn("私有仓库", updater._http_error_message(missing))
        self.assertIn("404", updater._http_error_message(missing))

    def test_certificate_verify_failure_reports_friendly_tls_error(self) -> None:
        updater = HostUpdater(current_version="0.2.0")
        reasons = ["CNB 镜像", "GitHub"]
        for index, source in enumerate(updater.sources):
            with patch.object(
                updater, "_releases_from",
                side_effect=urllib.error.URLError(
                    ssl.SSLError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
                                 " (_ssl.c:1006)")),
            ):
                updater._check_worker(False)
            status = updater.status()
            self.assertEqual(status["state"], "check_failed")
            error = str(status["error"])
            self.assertIn(reasons[index], error)
            self.assertIn("HTTPS 证书校验失败", error)

    def test_download_certificate_failure_reports_friendly_tls_error(self) -> None:
        updater = HostUpdater(current_version="0.2.0")
        summary = _release("v0.3.0")
        failure = urllib.error.URLError(
            ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED")
        )
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "canhost-update-test"
            with patch("canhost.updater.cleanup_update_dirs", return_value=0), \
                 patch("canhost.updater.update_temp_dir", return_value=work), \
                 patch.object(updater, "_download_payload", side_effect=failure):
                updater._download_worker(summary)
        status = updater.status()
        self.assertEqual(status["state"], "download_failed")
        self.assertIn("HTTPS 证书校验失败", status["error"])

    def test_windows_dead_system_proxy_retries_direct_without_reboot(self) -> None:
        updater = HostUpdater(current_version="0.2.0")

        class Opener:
            def __init__(self, result=None, error=None):
                self.result = result
                self.error = error
                self.calls = 0

            def open(self, request, timeout):
                self.calls += 1
                if self.error:
                    raise self.error
                return self.result

        refused = urllib.error.URLError(OSError(10061, "actively refused"))
        expected = object()
        proxy_opener = Opener(error=refused)
        direct_opener = Opener(result=expected)
        with patch("canhost.updater.os.name", "nt"), \
             patch("canhost.updater.urllib.request.getproxies",
                   return_value={"https": "http://127.0.0.1:7890"}), \
             patch("canhost.updater.urllib.request.build_opener",
                   side_effect=[proxy_opener, direct_opener]) as build:
            actual = updater._open_url(urllib.request.Request("https://cnb.cool/test"))

        self.assertIs(actual, expected)
        self.assertEqual(proxy_opener.calls, 1)
        self.assertEqual(direct_opener.calls, 1)
        self.assertEqual(build.call_count, 2)
        direct_proxy_handler = build.call_args_list[1].args[0]
        self.assertEqual(direct_proxy_handler.proxies, {})

    def test_windows_direct_retry_rebuilds_proxy_mutated_https_request(self) -> None:
        updater = HostUpdater(current_version="0.2.0")
        original = urllib.request.Request(
            "https://cnb.cool/totok22/can-host/latest.json",
            headers={"Accept": "application/json", "X-Test": "kept"},
        )
        expected = object()

        class ProxyOpener:
            def open(self, request, timeout):
                request.add_header("Proxy-Authorization", "Basic must-not-leak")
                request.set_proxy("127.0.0.1:7890", "https")
                raise urllib.error.URLError(OSError(10061, "actively refused"))

        class DirectOpener:
            def open(self, request, timeout):
                self.request = request
                return expected

        direct_opener = DirectOpener()
        with patch("canhost.updater.os.name", "nt"), \
             patch("canhost.updater.urllib.request.getproxies",
                   return_value={"https": "http://127.0.0.1:7890"}), \
             patch("canhost.updater.urllib.request.build_opener",
                   side_effect=[ProxyOpener(), direct_opener]):
            actual = updater._open_url(original)

        self.assertIs(actual, expected)
        self.assertIsNot(direct_opener.request, original)
        self.assertEqual(direct_opener.request.full_url, original.full_url)
        self.assertEqual(direct_opener.request.host, "cnb.cool")
        self.assertIsNone(direct_opener.request._tunnel_host)
        direct_headers = {name.lower(): value for name, value in direct_opener.request.header_items()}
        self.assertEqual(direct_headers["accept"], "application/json")
        self.assertEqual(direct_headers["x-test"], "kept")
        self.assertNotIn("proxy-authorization", direct_headers)

    def test_windows_dead_proxy_and_failed_direct_reports_actionable_error(self) -> None:
        updater = HostUpdater(current_version="0.2.0")

        class FailingOpener:
            def __init__(self, error):
                self.error = error

            def open(self, request, timeout):
                raise self.error

        refused = urllib.error.URLError(OSError(10061, "actively refused"))
        direct_failure = urllib.error.URLError("network unreachable")
        with patch("canhost.updater.os.name", "nt"), \
             patch("canhost.updater.urllib.request.getproxies",
                   return_value={"https": "http://127.0.0.1:7890"}), \
             patch("canhost.updater.urllib.request.build_opener",
                   side_effect=[FailingOpener(refused), FailingOpener(direct_failure)]):
            with self.assertRaises(urllib.error.URLError) as caught:
                updater._open_url(urllib.request.Request("https://cnb.cool/test"))

        self.assertIn("系统代理拒绝连接", str(caught.exception))
        self.assertIn("network unreachable", str(caught.exception))

    def test_windows_dead_proxy_preserves_direct_http_error(self) -> None:
        updater = HostUpdater(current_version="0.2.0")

        class FailingOpener:
            def __init__(self, error):
                self.error = error

            def open(self, request, timeout):
                raise self.error

        refused = urllib.error.URLError(OSError(10061, "actively refused"))
        forbidden = urllib.error.HTTPError(
            "https://api.github.com/test", 403, "Forbidden", None, None
        )
        with patch("canhost.updater.os.name", "nt"), \
             patch("canhost.updater.urllib.request.getproxies",
                   return_value={"https": "http://127.0.0.1:7890"}), \
             patch("canhost.updater.urllib.request.build_opener",
                   side_effect=[FailingOpener(refused), FailingOpener(forbidden)]):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                updater._open_url(urllib.request.Request("https://api.github.com/test"))

        self.assertEqual(caught.exception.code, 403)

    def test_non_windows_connection_refusal_does_not_bypass_proxy(self) -> None:
        updater = HostUpdater(current_version="0.2.0")

        class FailingOpener:
            def open(self, request, timeout):
                raise urllib.error.URLError(OSError(10061, "actively refused"))

        with patch("canhost.updater.os.name", "posix"), \
             patch("canhost.updater.urllib.request.getproxies",
                   return_value={"https": "http://127.0.0.1:7890"}), \
             patch("canhost.updater.urllib.request.build_opener",
                   return_value=FailingOpener()) as build:
            with self.assertRaises(urllib.error.URLError):
                updater._open_url(urllib.request.Request("https://cnb.cool/test"))

        build.assert_called_once()

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

    def test_macos_download_worker_uses_update_zip_not_dmg(self) -> None:
        updater = HostUpdater(current_version="0.9.11")
        tag = "v0.9.12"
        update_name = f"{APP_FOLDER_NAME}_macOS_arm64_{tag}-update.zip"
        latest = _release(tag)
        latest["assets"].extend([
            {"name": f"{APP_FOLDER_NAME}_macOS_arm64_{tag}.dmg", "size": 11,
             "url": "https://example/app.dmg"},
            {"name": update_name, "size": 0, "url": "https://example/mac-update.zip"},
            {"name": f"{update_name}.sha256", "size": 95,
             "url": "https://example/mac-update.zip.sha256"},
        ])
        downloaded: list[str] = []

        def fake_download(asset, target, progress=False):
            downloaded.append(str(asset["name"]))
            target.parent.mkdir(parents=True, exist_ok=True)
            if str(asset["name"]).endswith(".sha256"):
                target.write_text(f"{hashlib.sha256(b'').hexdigest()}  {update_name}\n",
                                  encoding="utf-8")
            else:
                target.write_bytes(b"")

        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "canhost-update-mac"
            with patch("canhost.updater.update_platform", return_value="macos"), \
                 patch("canhost.updater.cleanup_update_dirs", return_value=0), \
                 patch("canhost.updater.update_temp_dir", return_value=work), \
                 patch("canhost.updater.extract_update_archive",
                       return_value=work / MAC_APP_BUNDLE_NAME) as extract, \
                 patch.object(updater, "_download_payload", side_effect=fake_download):
                updater._download_worker(latest)

        self.assertEqual(downloaded, [update_name, f"{update_name}.sha256"])
        self.assertEqual(updater.status()["state"], "ready")
        extract.assert_called_once_with(work / update_name, work, platform="macos")

    def test_default_repo_is_github_org_repo(self) -> None:
        self.assertEqual(DEFAULT_REPO, "BITFSAE/can-host")
        self.assertEqual(DEFAULT_CNB_REPO, "totok22/can-host")

    def test_check_records_notes_and_recent_releases_for_the_dialog(self) -> None:
        """升级后的弹窗和版本历史都读检查阶段留下的条目与列表。"""
        updater = HostUpdater(current_version="0.2.0")
        releases = [_release("v0.3.0"), _release("v0.2.1"), _release("v0.2.0")]
        recorded: list[dict] = []
        with patch.object(updater, "_releases_from", return_value=releases), \
             patch("canhost.updater.remember_release_record", side_effect=recorded.append):
            updater._check_worker(False)
        status = updater.status()
        self.assertEqual(status["latest"]["changes"], ["修复弹窗说明"])
        self.assertEqual([item["tag_name"] for item in status["history"]],
                         ["v0.3.0", "v0.2.1", "v0.2.0"])
        self.assertEqual(recorded[0]["changes"], ["修复弹窗说明"])

    def test_release_selection_uses_highest_version_not_remote_order(self) -> None:
        releases = [_release("v0.2.0"), _release("v0.4.0"), _release("v0.3.0")]
        selected = HostUpdater._select_release(releases, False)
        self.assertEqual(selected["tag_name"], "v0.4.0")

    def test_release_selection_skips_unrecognized_tag_without_hiding_valid_release(self) -> None:
        releases = [_release("nightly"), _release("v0.4.0")]
        selected = HostUpdater._select_release(releases, False)
        self.assertEqual(selected["tag_name"], "v0.4.0")

    def test_history_skips_drafts_and_omits_the_release_body(self) -> None:
        """历史列表只带条目：它每秒随轮询重发，带正文会把载荷抬高三倍。"""
        drafts = [{"tag_name": "v0.4.0", "draft": True, "assets": []},
                  {**_release("v0.3.0"), "body": "x" * 9000}]
        history = HostUpdater._history(drafts)
        self.assertEqual([item["tag_name"] for item in history], ["v0.3.0"])
        self.assertNotIn("body", history[0])
        # 覆盖成长正文后没有条目，说明列表确实只读条目、不再搬运正文。
        self.assertEqual(history[0]["changes"], [])
        self.assertEqual(HostUpdater._history([_release("v0.3.0")])[0]["changes"],
                         ["修复弹窗说明"])

    def test_release_record_is_scoped_to_the_released_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            with patch("canhost.updater.settings_path", return_value=settings):
                remember_release_record({"tag_name": "v0.3.0", "name": "v0.3.0",
                                         "changes": ["新条目"], "html_url": "https://example/v0.3.0"})
                self.assertEqual(release_record_for("0.3.0")["notes"], ["新条目"])
                # 预发布标签回落到同一版本的正式条目上。
                self.assertEqual(release_record_for("v0.3.0-rc1")["notes"], ["新条目"])
                self.assertEqual(release_record_for("0.2.9"), {})


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


class CnbMirrorSourceTest(IsolatedSettingsTestCase):
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

    def test_check_reconciles_both_sources_and_prefers_cnb_for_same_tag(self) -> None:
        updater = HostUpdater(current_version="0.2.0")
        urls: list[str] = []

        def fake_fetch(url, headers=None):
            urls.append(url)
            self.assertEqual(headers.get("Cache-Control"), "no-cache")
            if "cnb.cool" in url:
                self.assertNotIn("Authorization", headers or {})
                return _cnb_channel("v0.3.0")
            return [_release("v0.3.0")]

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
        self.assertEqual(len(urls), 2)
        self.assertTrue(any("cnb.cool" in url for url in urls))
        self.assertTrue(any("api.github.com" in url for url in urls))

    def test_check_detects_github_release_while_cnb_mirror_is_stale(self) -> None:
        updater = HostUpdater(current_version="0.3.0")

        def fake_fetch(url, headers=None):
            if "cnb.cool" in url:
                return _cnb_channel("v0.3.0")
            return [_release("v0.4.0")]

        with patch.object(updater, "_fetch_json", side_effect=fake_fetch):
            updater._check_worker(False)
        status = updater.status()
        self.assertEqual(status["state"], "update_available")
        self.assertEqual(status["source"], SOURCE_GITHUB)
        self.assertEqual(status["latest"]["tag_name"], "v0.4.0")

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

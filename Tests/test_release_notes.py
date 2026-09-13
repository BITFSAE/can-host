"""版本说明解析、随包说明和更新完成状态的测试。"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from canhost import release_notes
from canhost.updater import installed_update_state, version_key

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"

CHANGELOG = """# 变更记录

## 未发布

- 尚未发布的条目

## v0.9.5 — 2026-09-13

- 新增遥测数据模拟页面。
- 修复 macOS 检查更新报证书错误。

## v0.9.4 — 2026-09-12

- 弹窗支持点击遮罩关闭。
"""

RELEASE_BODY = """# BITFSAE CAN Host 0.9.6 · 2026-09-14

## 本次更新

- 新增更新完成弹窗。
- 修复历史说明缺失。

## 下载

- `BITFSAE_CAN_Host_v0.9.6.zip`
"""


class ChangelogParseTest(unittest.TestCase):
    def test_release_sections_ignore_unreleased_and_keep_dates(self) -> None:
        sections = [item["version"] for item in release_notes.release_sections(CHANGELOG)]
        self.assertEqual(sections, ["0.9.5", "0.9.4"])
        newest = release_notes.newest_release(CHANGELOG)
        self.assertEqual(newest["date"], "2026-09-13")
        self.assertEqual(len(newest["notes"]), 2)

    def test_newest_release_follows_version_not_file_order(self) -> None:
        text = "## v0.9.2 — 2026-09-01\n\n- 旧\n\n## v0.10.0 — 2026-09-02\n\n- 新\n"
        self.assertEqual(release_notes.newest_release(text)["version"], "0.10.0")

    def test_notes_for_prerelease_tag_fall_back_to_base_version(self) -> None:
        self.assertEqual(len(release_notes.notes_for_version(CHANGELOG, "v0.9.5-rc1")), 2)
        self.assertEqual(release_notes.notes_for_version(CHANGELOG, "0.0.1"), [])

    def test_notes_are_flat_lines_not_nested_bullets(self) -> None:
        text = "## v1.0.0 — 2026-01-01\n\n- 顶层条目\n  - 子条目\n"
        self.assertEqual(release_notes.notes_for_version(text, "1.0.0"), ["顶层条目"])


class ReleaseBodyTest(unittest.TestCase):
    def test_prefers_update_section_over_download_section(self) -> None:
        notes = release_notes.release_notes_from_body(RELEASE_BODY)
        self.assertEqual(notes, ["新增更新完成弹窗。", "修复历史说明缺失。"])

    def test_falls_back_to_first_section_for_older_bodies(self) -> None:
        body = "# 标题\n\n## 变更\n\n- 只有一条\n"
        self.assertEqual(release_notes.release_notes_from_body(body), ["只有一条"])

    def test_body_without_bullets_yields_nothing(self) -> None:
        self.assertEqual(release_notes.release_notes_from_body("Windows 安装说明。"), [])


class EmbeddedNotesTest(unittest.TestCase):
    def test_release_info_module_is_generated_for_a_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            # 写入临时副本，避免测试覆盖仓库里发布时生成的 release_info.py。
            (work / "canhost").mkdir()
            (work / "canhost" / "__init__.py").write_text(
                '__version__ = "0.0.0"\n__version_date__ = "1970-01-01"\n', encoding="utf-8")
            changelog = work / "CHANGELOG.md"
            changelog.write_text(CHANGELOG, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "set_version.py"), "v0.9.5",
                 "--changelog", str(changelog), "--package-root", str(work)],
                capture_output=True, text=True, cwd=str(ROOT),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            generated = (work / "canhost" / "release_info.py").read_text(encoding="utf-8")
            self.assertIn("RELEASE_VERSION = '0.9.5'", generated)
            self.assertIn("RELEASE_DATE = '2026-09-13'", generated)
            self.assertIn("RELEASE_HISTORY", generated)
            self.assertIn('__version__ = "0.9.5"',
                          (work / "canhost" / "__init__.py").read_text(encoding="utf-8"))
            namespace: dict[str, object] = {}
            exec(compile(generated, "release_info.py", "exec"), namespace)
            self.assertEqual(len(namespace["RELEASE_NOTES"]), 2)
            self.assertEqual(namespace["RELEASE_HISTORY"][1]["version"], "0.9.4")

    def test_history_prefers_embedded_then_repository_changelog(self) -> None:
        embedded = [{"version": "1.0.0", "date": "2026-01-01", "notes": ["a"]}]
        with patch.object(release_notes, "embedded_history", return_value=embedded):
            self.assertEqual(release_notes.history(), embedded)
        with patch.object(release_notes, "embedded_history", return_value=[]), \
             patch.object(release_notes, "changelog_text", return_value=CHANGELOG):
            versions = [item["version"] for item in release_notes.history()]
        self.assertEqual(versions, ["0.9.5", "0.9.4"])

    def test_notes_for_release_reports_source(self) -> None:
        embedded = {"version": "0.9.5", "date": "2026-09-13", "notes": ["随包条目"]}
        with patch.object(release_notes, "embedded_release", return_value=embedded):
            self.assertEqual(release_notes.notes_for_release("0.9.5"), (["随包条目"], "embedded"))
            self.assertEqual(release_notes.notes_for_release("0.9.5", ["缓存条目"])[0], ["缓存条目"])

    def test_release_date_falls_back_to_changelog(self) -> None:
        with patch.object(release_notes, "embedded_release",
                          return_value={"version": "", "date": "", "notes": []}), \
             patch.object(release_notes, "changelog_text", return_value=CHANGELOG):
            self.assertEqual(release_notes.release_date_for("0.9.4"), "2026-09-12")


class InstalledUpdateStateTest(unittest.TestCase):
    """升级判定读的是上次成功启动的版本标记，安装包和软件内更新共用。"""

    def _state(self, current: str, previous: str, notes: list[str] | None = None):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "installed-version.json"
            if previous:
                marker.write_text(json.dumps({"version": previous}), encoding="utf-8")
            with patch("canhost.updater.installed_version_path", return_value=marker), \
                 patch("canhost.updater.release_record_for",
                       return_value={"tag_name": "v" + current, "notes": notes or []}), \
                 patch.object(release_notes, "release_date_for", return_value="2026-09-14"):
                return installed_update_state(current)

    def test_detects_in_place_upgrade_and_loads_cached_notes(self) -> None:
        state = self._state("0.9.6", "0.9.5", notes=["缓存条目"])
        self.assertTrue(state["upgraded"])
        self.assertEqual(state["previous_version"], "0.9.5")
        self.assertEqual(state["notes"], ["缓存条目"])
        self.assertTrue(state["release_url"].endswith("/releases/tag/v0.9.6"))

    def test_same_version_launch_reports_nothing(self) -> None:
        state = self._state("0.9.6", "0.9.6")
        self.assertFalse(state["upgraded"])
        self.assertEqual(state["notes"], [])

    def test_first_launch_is_not_reported_as_an_upgrade(self) -> None:
        state = self._state("0.9.6", "")
        self.assertTrue(state["first_launch"])
        self.assertFalse(state["upgraded"])

    def test_downgrade_is_not_reported_as_an_upgrade(self) -> None:
        state = self._state("0.9.5", "0.9.6")
        self.assertFalse(state["upgraded"])
        self.assertEqual(state["notes"], [])

    def test_version_key_orders_prereleases_below_finals(self) -> None:
        self.assertGreater(version_key("v0.9.6"), version_key("v0.9.6-rc1"))
        self.assertGreater(version_key("v0.10.0"), version_key("v0.9.9"))
        self.assertGreater(release_notes.version_sort_key("0.10.0"),
                           release_notes.version_sort_key("0.9.9"))


class ReleaseNotesScriptTest(unittest.TestCase):
    def test_generates_markdown_and_json_for_a_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            changelog = work / "CHANGELOG.md"
            changelog.write_text(CHANGELOG, encoding="utf-8")
            markdown = work / "notes.md"
            payload = work / "notes.json"
            result = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "release_notes.py"),
                 "--version", "v0.9.5", "--changelog", str(changelog),
                 "--output", str(markdown), "--json-output", str(payload)],
                capture_output=True, text=True, cwd=str(ROOT),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            text = markdown.read_text(encoding="utf-8")
            self.assertIn("# BITFSAE CAN Host 0.9.5 · 2026-09-13", text)
            self.assertIn("## 本次更新", text)
            self.assertIn("BITFSAE_CAN_Host_v0.9.5_setup.exe", text)
            self.assertIn("libPCBUSB", text)
            self.assertEqual(json.loads(payload.read_text(encoding="utf-8")),
                             ["新增遥测数据模拟页面。", "修复 macOS 检查更新报证书错误。"])


if __name__ == "__main__":
    unittest.main()

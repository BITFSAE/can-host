"""更新界面与 JavaScript API 的静态一致性检查。

前端是零构建的多 script 直载，元素 id 只在 `index.html` 里声明，写错不会在
导入期报错，只有在用户点开弹窗时才失败。这里用源码级检查把它挡在构建前。
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "canhost" / "web"


class FrontendIdTest(unittest.TestCase):
    def test_update_scripts_only_reference_ids_declared_in_index(self) -> None:
        html = (WEB / "index.html").read_text(encoding="utf-8")
        declared = set(re.findall(r'id="([^"]+)"', html))
        for name in ("updater.js", "core.js"):
            script = (WEB / "js" / name).read_text(encoding="utf-8")
            referenced = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', script))
            referenced |= set(re.findall(r'text\("#([A-Za-z0-9_-]+)"', script))
            missing = sorted(referenced - declared)
            self.assertEqual(missing, [], f"{name} 引用了 index.html 中不存在的元素：{missing}")

    def test_update_dialogs_apply_the_shared_modal_structure(self) -> None:
        html = (WEB / "index.html").read_text(encoding="utf-8")
        for dialog_id in ("updaterDialog", "updateResultDialog", "releaseHistoryDialog"):
            with self.subTest(dialog=dialog_id):
                block = html.split(f'id="{dialog_id}"', 1)[1].split("</dialog>", 1)[0]
                self.assertIn("class=\"modal", block)
                self.assertIn("modal-body", block)
                self.assertIn("<footer>", block)

    def test_startup_reports_the_completed_update(self) -> None:
        core = (WEB / "js" / "core.js").read_text(encoding="utf-8")
        updater = (WEB / "js" / "updater.js").read_text(encoding="utf-8")
        self.assertIn("reportStartupUpdate", core)
        self.assertIn("startup_update_state", core)
        self.assertIn("function showUpdateResult", updater)
        # 完成弹窗只应出现一次，避免轮询重复弹出。
        self.assertIn("updateResultShown", updater)

    def test_release_notes_are_rendered_per_item(self) -> None:
        updater = (WEB / "js" / "updater.js").read_text(encoding="utf-8")
        self.assertIn("function renderReleaseNotes", updater)
        self.assertIn("changes", updater)
        self.assertNotIn("notes.textContent = latest?.body", updater)


class ReleasePageApiTest(unittest.TestCase):
    def _api(self):
        from canhost.app import Api

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        with patch("canhost.updater.settings_path", return_value=root / "settings.json"), \
             patch("canhost.updater.installed_version_path", return_value=root / "installed.json"):
            return Api()

    def test_rejects_non_project_urls(self) -> None:
        api = self._api()
        for url in ("http://github.com/x", "file:///etc/passwd", "", "https://evil.example/x"):
            with self.subTest(url=url):
                self.assertFalse(api.open_release_page(url)["ok"])

    def test_opens_only_https_project_hosts(self) -> None:
        api = self._api()
        opened: list[str] = []
        with patch("webbrowser.open", side_effect=opened.append):
            self.assertTrue(api.open_release_page("https://github.com/BITFSAE/can-host/releases")["ok"])
            self.assertTrue(api.open_release_page("https://cnb.cool/totok22/can-host/-/releases")["ok"])
        self.assertEqual(len(opened), 2)

    def test_release_history_merges_online_release_over_embedded(self) -> None:
        api = self._api()
        offline = api.release_history()
        self.assertTrue(offline["entries"], "源码运行时应能从 CHANGELOG.md 读到版本历史")
        current = offline["current_version"]
        self.assertEqual(offline["entries"][0]["version"], current)

        api._updater._state["history"] = [{
            "tag_name": f"v{current}", "published_at": "2026-09-14T00:00:00Z",
            "html_url": "https://github.com/BITFSAE/can-host/releases/tag/x",
            "changes": ["在线条目"], "prerelease": False,
        }]
        merged = api.release_history(True)
        self.assertTrue(merged["online"])
        self.assertEqual(merged["entries"][0]["notes"], ["在线条目"])
        self.assertEqual(merged["entries"][0]["source"], "release")
        # 历史条目每秒随状态轮询重发，只保留扫读需要的字段。
        self.assertEqual(
            sorted(merged["entries"][0]),
            ["date", "notes", "prerelease", "source", "url", "version"],
        )


if __name__ == "__main__":
    unittest.main()

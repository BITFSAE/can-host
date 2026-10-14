"""Wiring between the DBC page, its scripts and the backend API."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

from canhost.app import Api
from canhost.dbc.layout import INTEL, MOTOROLA, signal_bits

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "canhost" / "web"
JS = WEB / "js"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
CORE = (JS / "core.js").read_text(encoding="utf-8")
DBC_SCRIPTS = ("dbc.js", "dbc-plot.js", "dbc-editor.js", "dbc-tools.js")
SOURCES = {name: (JS / name).read_text(encoding="utf-8") for name in DBC_SCRIPTS}


def layout_cases() -> list[dict]:
    cases = []
    for order in (INTEL, MOTOROLA):
        for start in range(0, 64, 3):
            for length in (1, 2, 4, 8, 11, 16, 20, 32):
                cases.append({"start": start, "length": length, "order": order,
                              "bits": signal_bits(start, length, order)})
    return cases


class PageWiringTests(unittest.TestCase):
    def test_page_nav_and_scripts_are_registered_in_order(self) -> None:
        self.assertIn('id="page-dbc"', HTML)
        self.assertIn('data-page="dbc"', HTML)
        scripts = re.findall(r'<script src="([^"]+)"', HTML)
        for name in DBC_SCRIPTS:
            self.assertIn(f"js/{name}", scripts)
        self.assertLess(scripts.index("js/monitor.js"), scripts.index("js/dbc.js"))
        self.assertLess(scripts.index("vendor/uplot/uPlot.iife.min.js"), scripts.index("js/dbc-plot.js"))
        self.assertLess(scripts.index("js/dbc.js"), scripts.index("js/dbc-editor.js"))
        self.assertIn("vendor/uplot/uPlot.min.css", HTML)

    def test_new_page_is_appended_so_alt_shortcuts_keep_their_pages(self) -> None:
        order = re.search(r"const PAGE_ORDER = \[([^\]]*)\]", CORE).group(1)
        pages = [item.strip().strip('"') for item in order.split(",")]
        self.assertEqual(pages[:9], ["overview", "cells", "alarms", "control", "vehicle", "fan", "frames", "bench", "ivt"])
        self.assertEqual(pages[-1], "dbc")
        self.assertNotIn("dbc", re.search(r"const TOOL_PAGES = \[([^\]]*)\]", CORE).group(1))

    def test_polling_and_rendering_hooks_exist(self) -> None:
        self.assertIn("bindDbcControls();", CORE)
        self.assertIn("dbc_live", CORE)
        self.assertIn("renderDbc();", CORE)
        self.assertRegex(CORE, r'page === "dbc" && dbcUi\.source === "vehicle"')

    def test_every_backend_call_the_scripts_make_exists(self) -> None:
        used = set()
        for name, script in SOURCES.items():
            used |= set(re.findall(r"api\??\.(dbc_\w+)", script))
        exposed = {name for name in dir(Api) if name.startswith("dbc")}
        self.assertTrue(used)
        self.assertEqual(sorted(used - exposed), [], "脚本调用了后端没有的接口")
        # Everything the backend exposes is used by the page (nothing dead in the bridge).
        self.assertEqual(sorted(exposed - used), [], "后端暴露了页面不用的接口")

    def test_every_element_the_scripts_look_up_exists(self) -> None:
        html_ids = set(re.findall(r'id="([^"]+)"', HTML))
        made_by_scripts = set()
        for script in SOURCES.values():
            made_by_scripts |= set(re.findall(r'id="([A-Za-z][\w-]*)"', script))
        missing = []
        for name, script in SOURCES.items():
            for identifier in set(re.findall(r'\$\$?\(["\'`]#([A-Za-z][\w-]*)', script)):
                if identifier.endswith("_"):      # a template prefix such as dbcField_${name}
                    continue
                if identifier not in html_ids and identifier not in made_by_scripts:
                    missing.append(f"{name}: #{identifier}")
        self.assertEqual(missing, [])

    def test_scripts_do_not_define_a_second_frame_format(self) -> None:
        """The DBC page is for viewing: it must not duplicate project frame definitions."""
        for name, script in SOURCES.items():
            self.assertNotRegex(script, r"0x4B0|0x5A[0-9A-F]|0x18FF", name)

    def test_vendored_plot_library_keeps_its_license_and_provenance(self) -> None:
        folder = WEB / "vendor" / "uplot"
        for name in ("uPlot.iife.min.js", "uPlot.min.css", "LICENSE", "README.md"):
            self.assertTrue((folder / name).is_file(), name)
        script = (folder / "uPlot.iife.min.js").read_text(encoding="utf-8")
        self.assertIn("v1.6.32", script[:200])
        for forbidden in ("fetch(", "XMLHttpRequest", "eval(", "WebSocket", "sendBeacon"):
            self.assertNotIn(forbidden, script)
        self.assertIn("MIT", (folder / "LICENSE").read_text(encoding="utf-8"))

    def test_styles_use_the_design_tokens(self) -> None:
        css = (WEB / "styles.css").read_text(encoding="utf-8")
        block = css[css.index("DBC 工作台\n"):]
        self.assertNotRegex(block, r"border-radius:\s*\d+px")
        self.assertNotRegex(block, r"font-size:\s*\d+(\.\d+)?px")
        self.assertNotRegex(block, r"font:\s*[0-9]+\s+\d+(\.\d+)?px")
        for index in range(1, 9):
            self.assertIn(f"--series-{index}:", css)
            self.assertIn(f"--sigbg-{index}:", css)
        self.assertEqual(css.count(":root[data-theme=\"light\"] {\n  --series-1"), 1)


@unittest.skipUnless(shutil.which("node"), "需要 node 运行页面脚本")
class ScriptTests(unittest.TestCase):
    def test_scripts_parse(self) -> None:
        for name in DBC_SCRIPTS:
            result = subprocess.run([shutil.which("node"), "--check", str(JS / name)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}")

    def test_layout_parity_display_rules_and_polling(self) -> None:
        result = subprocess.run(
            [shutil.which("node"), str(ROOT / "Tests" / "dbc_ui_smoke.js")],
            input=json.dumps(layout_cases()), capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("dbc ui smoke ok", result.stdout)


class DropTests(unittest.TestCase):
    def test_only_dbc_files_dropped_on_the_window_are_opened(self) -> None:
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            settings = Path(folder) / "settings.json"
            good = Path(folder) / "Vehicle_Can1.dbc"
            shutil.copy(Path(__file__).parent / "fixtures" / "dbc" / "sample.dbc", good)
            other = Path(folder) / "notes.txt"
            other.write_text("not a dbc")
            with patch("canhost.updater.settings_path", return_value=settings):
                api = Api()
                try:
                    paths = api._handle_file_drop({"dataTransfer": {"files": [
                        {"pywebviewFullPath": str(good)}, {"pywebviewFullPath": str(other)}, {"name": "no-path.dbc"}]}})
                    self.assertEqual(paths, [str(good)])
                    self.assertEqual([f["name"] for f in api.dbc_state()["files"]], ["Vehicle_Can1.dbc"])
                    self.assertEqual(api._handle_file_drop(None), [])
                    self.assertEqual(api._handle_file_drop({"dataTransfer": None}), [])
                finally:
                    api.close()


if __name__ == "__main__":
    unittest.main()

"""Wiring between the encoder page, its script and the backend API."""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import unittest

from canhost import decoders as d
from canhost.app import Api
from canhost.encoder.service import POLL_RATES_HZ, EncoderService

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "canhost" / "web"
SCRIPT = (WEB / "js" / "encoder.js").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
CORE = (WEB / "js" / "core.js").read_text(encoding="utf-8")
CSS = (WEB / "styles.css").read_text(encoding="utf-8")
PAGE = HTML[HTML.index('<section class="page" id="page-encoder"'):HTML.index('<section class="page" id="page-simulator"')]


def commands(pattern: str) -> set[str]:
    return set(re.findall(pattern, SCRIPT))


class PageWiringTest(unittest.TestCase):
    def test_page_is_a_tool_page_appended_after_the_numbered_shortcuts(self) -> None:
        order = re.search(r"const PAGE_ORDER = \[([^\]]*)\]", CORE).group(1)
        pages = [item.strip().strip('"') for item in order.split(",")]
        self.assertEqual(pages[:9], ["overview", "cells", "alarms", "control", "vehicle", "fan", "frames", "bench", "ivt"])
        self.assertIn("encoder", pages[9:])
        self.assertIn('"encoder"', re.search(r"const TOOL_PAGES = \[[^\]]*\]", CORE).group(0))
        self.assertIn('data-page="encoder"', HTML)
        self.assertIn('id="page-encoder"', HTML)

    def test_script_loads_after_core_and_the_chart_library(self) -> None:
        scripts = re.findall(r'<script src="([^"]+)"', HTML)
        self.assertLess(scripts.index("js/core.js"), scripts.index("js/encoder.js"))
        self.assertLess(scripts.index("vendor/uplot/uPlot.iife.min.js"), scripts.index("js/encoder.js"))

    def test_core_calls_every_hook_the_page_needs(self) -> None:
        for hook in ("bindEncoderControls();", "initEncoder();", "renderEncoder();", "get_encoder_snapshot",
                     "encFetchSeries", "pendingEncoderAction", "encPlotsRebuild", '"#encChannelSelect"'):
            self.assertIn(hook, CORE)

    def test_every_backend_call_the_page_makes_exists_on_the_api(self) -> None:
        calls = commands(r"state\.api\.([a-z_]+)\(")
        self.assertTrue({"connect_encoder", "disconnect_encoder", "encoder_command", "get_encoder_series",
                         "choose_export_encoder_csv"} <= calls)
        for name in calls:
            self.assertTrue(callable(getattr(Api, name, None)), name)
        self.assertIn("get_encoder_snapshot", CORE)
        self.assertTrue(callable(Api.get_encoder_snapshot))

    def test_plain_actions_never_try_to_send_a_write(self) -> None:
        """encRun/encRead call the backend without acknowledgement; a write routed there would always be refused."""
        plain = commands(r'enc(?:Run|Read)\("([a-z_]+)"') | commands(r'"(read_[a-z]+)"')
        self.assertGreaterEqual(len(plain), 8)
        for name in plain:
            self.assertIn(name, EncoderService._COMMANDS, name)
            self.assertNotEqual(EncoderService._COMMANDS[name][0], "write", name)

    def test_writes_go_through_the_confirmation_dialog(self) -> None:
        written = commands(r'encWrite\("([a-z_]+)"')
        self.assertEqual(written, {name for name, (tier, _) in EncoderService._COMMANDS.items() if tier == "write"})
        self.assertIn("encoder_command(name, args, true)", SCRIPT)
        # The only place that passes acknowledgement is the confirmation action.
        self.assertEqual(len(re.findall(r"encoder_command\([^)]*true\)", SCRIPT)), 1)
        branch = CORE[CORE.index("if (state.pendingEncoderAction)"):CORE.index("if (state.pendingIvtAction)")]
        self.assertIn("pending.run()", branch)
        self.assertIn("verify", branch)

    def test_every_element_the_script_touches_exists_in_the_page(self) -> None:
        identifiers = set(re.findall(r"#(enc[A-Za-z0-9]+)", SCRIPT))
        self.assertGreater(len(identifiers), 60)
        missing = sorted(name for name in identifiers if f'id="{name}"' not in PAGE)
        self.assertEqual(missing, [])

    def test_every_enc_class_is_styled(self) -> None:
        used = set(re.findall(r"(?<![\w-])(enc-[a-z]+(?:-[a-z]+)*)\b", PAGE + SCRIPT))
        self.assertGreater(len(used), 30)
        hooks = {"enc-reading", "enc-source", "enc-cal"}        # panel hooks with no rule of their own
        undefined = sorted(name for name in used - hooks if f".{name}" not in CSS)
        self.assertEqual(undefined, [])

    def test_choices_in_the_page_match_the_backend_limits(self) -> None:
        self.assertEqual(sorted(int(v) for v in re.findall(r'data-window="(\d+)"', PAGE)), [5, 10, 30, 60])
        windows = re.search(r"const ENC_WINDOWS = \[([^\]]*)\]", SCRIPT).group(1)
        self.assertEqual([int(v) for v in windows.split(",")], [5, 10, 30, 60])
        modes = re.search(r"const ENC_ANGLE_MODES = \[([^\]]*)\]", SCRIPT).group(1)
        self.assertEqual(re.findall(r'data-mode="([a-z]+)"', PAGE), [m.strip().strip('"') for m in modes.split(",")])
        default = int(re.search(r"pollHz: (\d+)", SCRIPT).group(1))
        self.assertIn(default, POLL_RATES_HZ)
        low, high = d.BRT_PERIOD_US_RANGE
        self.assertRegex(PAGE, rf'id="encPeriod" type="number" min="{low}" max="{high}"')
        self.assertIn(f"{low}–{high} µs", PAGE)
        self.assertRegex(PAGE, r'id="encCount" type="number" min="0" max="4095"')
        self.assertRegex(PAGE, r'id="encSampleMs" type="number" min="1" max="65535"')

    def test_labels_keep_the_two_zero_conventions_apart(self) -> None:
        for phrase in ("单圈角度", "累计角度", "原始计数", "上位机累计"):
            self.assertIn(phrase, PAGE + SCRIPT)

    def test_nothing_shows_zero_or_an_old_value_for_missing_data(self) -> None:
        self.assertIn("等待数据", SCRIPT)
        self.assertIn("isStaleData(reading.age", SCRIPT)
        self.assertIn("dataAgeText", SCRIPT)
        self.assertRegex(SCRIPT, r'if \(!reading\) \{[^}]*"等待数据"')

    def test_encoder_styles_use_only_the_shared_tokens(self) -> None:
        block = CSS[CSS.index("/* ---- BRT 编码器页"):]
        self.assertEqual(re.findall(r"font-size:\s*\d+px", block), [])
        self.assertEqual(re.findall(r"font:\s*(?:\d+\s+)?\d+px", block), [])
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,6}\b", block), [])

    def test_scripts_parse(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is needed for the syntax check")
        for name in ("encoder.js", "core.js"):
            result = subprocess.run([node, "--check", str(WEB / "js" / name)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_script_behaviour_in_a_minimal_dom(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is needed for the page smoke test")
        result = subprocess.run([node, str(ROOT / "Tests" / "encoder_ui_smoke.js")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("encoder ui smoke ok", result.stdout)


if __name__ == "__main__":
    unittest.main()

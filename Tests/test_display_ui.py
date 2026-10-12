"""Wiring between the driver-display page, its backend API and the shared shell."""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import unittest
from unittest.mock import patch

from canhost.app import Api
from canhost.display import model
from canhost.display.service import DisplayBenchService

WEB = Path(__file__).resolve().parents[1] / "canhost" / "web"
SCRIPT = (WEB / "js" / "display.js").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
CORE = (WEB / "js" / "core.js").read_text(encoding="utf-8")


def wheel_columns() -> list[str]:
    """Per-wheel fields of the ECU table, read from the page's own column list."""
    return re.findall(r'\["([a-z_]+)", "[^"]*", [0-9.]+\]', SCRIPT)


def literal_keys() -> set[str]:
    """Every value key the page names, with the ${wheel}/${field}/${point} templates expanded."""
    keys = set(re.findall(r'(?:key|data-key)[=:] ?["`]?([a-z]+\.[a-z0-9_.${}]+)', SCRIPT))
    expanded: set[str] = set()
    for key in keys:
        if "${" not in key:
            expanded.add(key)
            continue
        for wheel in model.WHEELS:
            candidate = key.replace("${wheel}", wheel)
            fields = wheel_columns() + ["fault", "enabled"] if "${field}" in candidate else [None]
            for field in fields:
                expanded.add(candidate.replace("${field}", field) if field else
                             candidate.replace("${point}", "1").replace("${point + 1}", "1"))
    return expanded


class DisplayPageWiringTest(unittest.TestCase):
    def test_every_value_key_the_page_names_exists_in_the_backend_table(self) -> None:
        keys = literal_keys()
        self.assertGreater(len(keys), 30)
        missing = sorted(key for key in keys if key not in model.VALUE_SPECS)
        self.assertEqual(missing, [])

    def test_wheel_table_columns_are_real_fields(self) -> None:
        columns = wheel_columns()
        self.assertEqual(columns, ["torque_nm", "rpm", "motor_c", "inverter_c", "igbt_c"])
        for wheel in model.WHEELS:
            for field in columns:
                self.assertIn(f"wheel.{wheel}.{field}", model.VALUE_SPECS)

    def test_every_value_is_reachable_from_the_page(self) -> None:
        text = SCRIPT
        # Keys built from templates are covered by their template stems.
        stems = ("wheel.${wheel}.", "tire.${wheel}.t", "wheelsw")
        unreachable = [key for key in model.VALUE_SPECS
                       if key not in text and not any(key.startswith(stem.replace("${wheel}", w).replace("${wheel}", ""))
                                                      for stem in stems for w in model.WHEELS)]
        # wheelsw.* are driven by the wheel buttons through the backend commands, not by inputs.
        self.assertEqual([key for key in unreachable if not key.startswith("wheelsw")], [])

    def test_every_command_the_page_issues_is_implemented(self) -> None:
        names = set(re.findall(r'dbCommand\("([a-z_]+)"', SCRIPT))
        names |= set(re.findall(r'data-db-command(?:=|\\?=)\\?"([a-z_]+)', SCRIPT + HTML))
        names |= set(re.findall(r'data-db-command="([a-z_]+)"', HTML))
        self.assertGreaterEqual(len(names), 12)
        self.assertEqual(sorted(names - set(DisplayBenchService._COMMANDS)), [])

    def test_every_backend_call_exists_on_the_api(self) -> None:
        calls = set(re.findall(r"state\.api\.([a-z_]+)\(", SCRIPT))
        self.assertTrue({"connect_display_bench", "disconnect_display_bench", "display_bench_set",
                         "display_bench_command"} <= calls)
        for name in calls:
            self.assertTrue(callable(getattr(Api, name, None)), name)
        self.assertTrue(callable(Api.get_display_bench_snapshot))

    def test_ids_the_script_reads_exist_in_the_page_or_are_generated_by_it(self) -> None:
        ids = set(re.findall(r'\$\("#([A-Za-z0-9_]+)"\)', SCRIPT)) | set(re.findall(r'text\("#([A-Za-z0-9_]+)"', SCRIPT)) \
            | set(re.findall(r'dbSetRow\("#([A-Za-z0-9_]+)"', SCRIPT)) | set(re.findall(r'row\("#([A-Za-z0-9_]+)"', SCRIPT))
        generated = {"dbEcuPeriod", "dbEcuFollow"}   # emitted inside the ECU card template
        missing = sorted(identifier for identifier in ids
                         if f'id="{identifier}"' not in HTML and identifier not in generated
                         and f'id="{identifier}"' not in SCRIPT)
        self.assertEqual(missing, [])

    def test_every_return_row_says_which_frame_it_is_and_what_it_carries(self) -> None:
        rows = re.findall(r'<div title="([^"]*)"><dt>([^<]+)<small>([^<]+)</small></dt><dd id="(db[A-Za-z]+)"', HTML)
        self.assertEqual(len(rows), 10)
        for title, name, sub, identifier in rows:
            self.assertRegex(sub, r"^0x[0-9A-F]{3} · \S", identifier)   # frame ID, then what it is
            self.assertGreater(len(title), 12, identifier)
            self.assertNotRegex(name, r"0x", identifier)                  # the visible name is plain Chinese
        names = {name for _, name, _, _ in rows}
        self.assertTrue({"方向盘指令", "转发的驾驶模式"} <= names)

    def test_wheel_tab_observes_the_physical_wheel_from_canb_alone(self) -> None:
        self.assertIn("不用接 CAN2", HTML)
        for identifier in ("dbObserveRows", "dbKnobValue", "dbSlipValue", "dbLapValue", "dbClearValue", "dbObserveClear"):
            self.assertIn(f'id="{identifier}"', HTML)
        self.assertIn("wheel_observer", SCRIPT)
        self.assertIn("wheel_log_clear", SCRIPT)

    def test_wheel_tab_labels_explain_their_frames(self) -> None:
        for frame_id in ("0x310", "0x700", "0x701 / 0x702"):
            self.assertRegex(HTML, rf'<small>{re.escape(frame_id)} · [^<]+</small>')

    def test_every_frame_has_a_plain_note_shown_in_the_monitor(self) -> None:
        for spec in model.FRAMES:
            self.assertGreater(len(spec.note), 6, spec.key)
        self.assertIn("frame.note", SCRIPT)
        self.assertTrue(all(frame["note"] for frame in model.frame_catalog()))

    def test_events_name_the_frame_in_words(self) -> None:
        source = (Path(WEB).parent / "display" / "service.py").read_text(encoding="utf-8")
        for phrase in ("方向盘指令（0x703）", "驾驶模式请求（0x310）", "WHEEL_FRAME_NAMES[frame_id]"):
            self.assertIn(phrase, source)

    def test_page_is_registered_as_a_tool_page_with_its_own_connection_dialog(self) -> None:
        self.assertIn('"display"', re.search(r"const PAGE_ORDER = \[[^\]]*\]", CORE).group(0))
        self.assertIn('"display"', re.search(r"const TOOL_PAGES = \[[^\]]*\]", CORE).group(0))
        self.assertIn('id="page-display"', HTML)
        self.assertIn('data-page="display"', HTML)
        self.assertIn('id="displayBenchDialog"', HTML)
        self.assertLess(HTML.index("js/core.js"), HTML.index("js/display.js"))
        for hook in ("bindDisplayBenchControls();", "buildDisplayBenchStatics();", "renderDisplayBench();",
                     "get_display_bench_snapshot"):
            self.assertIn(hook, CORE)

    def test_existing_alt_shortcuts_keep_their_pages(self) -> None:
        order = re.search(r"const PAGE_ORDER = \[([^\]]*)\]", CORE).group(1)
        pages = [item.strip().strip('"') for item in order.split(",")]
        self.assertEqual(pages[:9], ["overview", "cells", "alarms", "control", "vehicle", "fan", "frames",
                                     "bench", "ivt"])

    def test_scripts_parse(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is needed for the syntax check")
        for name in ("display.js", "core.js"):
            result = subprocess.run([node, "--check", str(WEB / "js" / name)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_all_page_elements_use_only_shared_design_tokens(self) -> None:
        css = (WEB / "styles.css").read_text(encoding="utf-8")
        block = css[css.index("/* ---------- 车手屏联调 ---------- */"):]
        # Font sizes and spacing come from the token scale; raw px font sizes are not allowed.
        self.assertEqual(re.findall(r"font-size:\s*\d+px", block), [])
        self.assertEqual(re.findall(r"font:\s*(?:\d+\s+)?\d+px", block), [])


class DisplayApiTest(unittest.TestCase):
    def test_bootstrap_publishes_the_catalog_and_gates_preview_by_build(self) -> None:
        api = Api()
        try:
            bootstrap = api.bootstrap()
            self.assertTrue(bootstrap["display_bench_enabled"])
            self.assertTrue(bootstrap["display_bench_preview"])
            self.assertEqual(len(bootstrap["display_bench"]["frames"]), 19)
        finally:
            api.close()

    def test_release_build_has_no_preview_even_if_asked(self) -> None:
        with patch("canhost.app._simulation_available", return_value=False):
            api = Api()
        try:
            self.assertFalse(api.bootstrap()["display_bench_preview"])
            result = api.connect_display_bench({"preview": True, "confirmed": True})
            self.assertFalse(result["ok"])
            self.assertEqual(api.get_display_bench_snapshot()["connection"]["state"], "disconnected")
        finally:
            api.close()

    def test_api_round_trip_in_preview(self) -> None:
        api = Api()
        try:
            self.assertTrue(api.connect_display_bench({"preview": True})["ok"])
            self.assertTrue(api.display_bench_set({"bms.soc_pct": 55})["ok"])
            self.assertTrue(api.display_bench_command("scenario", {"name": "lowsoc"})["ok"])
            self.assertEqual(api.get_display_bench_snapshot()["values"]["bms.soc_pct"], 18)
            self.assertTrue(api.disconnect_display_bench()["ok"])
        finally:
            api.close()


if __name__ == "__main__":
    unittest.main()

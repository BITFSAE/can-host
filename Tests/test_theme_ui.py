"""Light/dark appearance behavior and zero-build frontend wiring."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from canhost.app import (
    Api,
    DARK_WINDOW_BACKGROUND,
    LIGHT_WINDOW_BACKGROUND,
    _initial_window_background,
)


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "canhost" / "web"


class ThemeFrontendTest(unittest.TestCase):
    def test_debug_simulation_button_is_hidden_until_bootstrap_enables_it(self) -> None:
        html = (WEB / "index.html").read_text(encoding="utf-8")
        self.assertRegex(
            html,
            r'class="bus-connect simulation-toggle hidden" id="simulationBusButton"',
        )
        core = (WEB / "js" / "core.js").read_text(encoding="utf-8")
        self.assertIn('$("#simulationBusButton")?.classList.toggle(', core)
        self.assertIn("state.bootstrap.simulation_enabled !== true", core)
        self.assertIn("state.bootstrap.vehicle_simulation_enabled !== true", core)

    def test_theme_is_applied_before_styles_and_has_an_accessible_toggle(self) -> None:
        html = (WEB / "index.html").read_text(encoding="utf-8")
        self.assertLess(html.index("canHostTheme"), html.index("styles.css"))
        self.assertIn('id="themeToggle"', html)
        self.assertIn('aria-label="切换到浅色模式"', html)

        core = (WEB / "js" / "core.js").read_text(encoding="utf-8")
        self.assertIn("function applyTheme", core)
        self.assertIn("set_theme_preference", core)
        self.assertIn('matchMedia("(prefers-reduced-motion: reduce)")', core)

    def test_light_palette_and_charts_use_shared_semantic_tokens(self) -> None:
        styles = (WEB / "styles.css").read_text(encoding="utf-8")
        self.assertIn(':root[data-theme="light"]', styles)
        for token in ("--bg", "--surface-1", "--text", "--ok", "--warn", "--fault",
                      "--action", "--chart-voltage", "--chart-grid"):
            self.assertIn(token, styles)

        for script_name in ("bms.js", "vehicle.js"):
            script = (WEB / "js" / script_name).read_text(encoding="utf-8")
            self.assertIn('cssVar("--chart-', script)


class ThemePreferenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = Path(self.directory.name) / "settings.json"
        self.patcher = patch("canhost.updater.settings_path", return_value=self.settings)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.api = Api.__new__(Api)
        self.api._preference_lock = threading.Lock()

    def test_preference_defaults_dark_and_preserves_other_settings(self) -> None:
        self.assertEqual(self.api.theme_preference(), "dark")
        self.settings.write_text(json.dumps({"github_token": "keep"}), encoding="utf-8")
        self.assertTrue(self.api.set_theme_preference("light")["ok"])
        self.assertEqual(self.api.theme_preference(), "light")
        payload = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(payload, {"github_token": "keep", "theme_mode": "light"})

    def test_invalid_preference_is_rejected(self) -> None:
        result = self.api.set_theme_preference("system")
        self.assertFalse(result["ok"])
        self.assertFalse(self.settings.exists())

    def test_native_background_matches_saved_theme(self) -> None:
        class StubApi:
            def __init__(self, mode: str) -> None:
                self.mode = mode

            def theme_preference(self) -> str:
                return self.mode

        self.assertEqual(_initial_window_background(StubApi("light")), LIGHT_WINDOW_BACKGROUND)
        self.assertEqual(_initial_window_background(StubApi("dark")), DARK_WINDOW_BACKGROUND)


if __name__ == "__main__":
    unittest.main()

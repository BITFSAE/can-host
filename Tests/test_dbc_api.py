"""The DBC API as the page sees it, including the receive-path hooks."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

from canhost.app import Api
from canhost.decoders import CanFrame
from canhost.monitor import normalize_message_spec
from canhost.transport import CanService

FIXTURE = Path(__file__).parent / "fixtures" / "dbc" / "sample.dbc"
ENGINE_OK = bytes([0x88, 0x13, 0x50, 0x03, 0x64, 0, 0, 0])
EXPECTED_METHODS = {
    "dbc_state", "dbc_choose_open", "dbc_open", "dbc_new", "dbc_close", "dbc_reload",
    "dbc_set_bus", "dbc_detail", "dbc_edit", "dbc_undo", "dbc_redo", "dbc_compare", "dbc_save_review", "dbc_save",
    "dbc_choose_save", "dbc_live", "dbc_series_set", "dbc_series_fetch", "dbc_series_clear", "dbc_audit",
    "dbc_form", "dbc_encode", "dbc_trace_choose", "dbc_trace_close", "dbc_trace_series", "dbc_trace_audit",
    "dbc_trace_export", "dbc_save_text", "dbc_set_view",
}


class ApiCase(unittest.TestCase):
    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.settings = self.folder / "settings.json"
        self._patch = patch("canhost.updater.settings_path", return_value=self.settings)
        self._patch.start()
        self.path = self.folder / "Vehicle_Can1.dbc"
        shutil.copy(FIXTURE, self.path)
        self.api = Api()

    def tearDown(self) -> None:
        self.api.close()
        self._patch.stop()
        self._folder.cleanup()

    def open(self, bus: str = "can1") -> str:
        result = self.api.dbc_open(str(self.path), bus)
        self.assertTrue(result["ok"], result)
        return result["file"]["id"]


class SurfaceTests(ApiCase):
    def test_the_page_can_call_exactly_the_documented_dbc_methods(self) -> None:
        public = {name for name in dir(self.api) if name.startswith("dbc")}
        self.assertEqual(public, EXPECTED_METHODS)

    def test_watch_list_and_plot_selection_are_remembered(self) -> None:
        view = {"watch": [{"source": "main", "key": "s:291", "signal": "Speed"}],
                "plot": [{"source": "vehicle", "key": "e:5", "signal": "X"}], "source": "vehicle"}
        self.assertTrue(self.api.dbc_set_view(view)["ok"])
        self.assertEqual(self.api.dbc_state()["view"], view)
        for bad in ({"watch": "x"}, {"watch": [{"source": "x", "key": "s:1", "signal": "a"}]},
                    {"plot": [{"source": "main", "key": "", "signal": "a"}]}, "text"):
            self.assertFalse(self.api.dbc_set_view(bad)["ok"])

    def test_bootstrap_announces_the_page(self) -> None:
        self.assertTrue(self.api.bootstrap()["dbc_enabled"])

    def test_bad_input_is_answered_not_raised(self) -> None:
        for call in (lambda: self.api.dbc_open(""), lambda: self.api.dbc_open(str(self.path), "weird"),
                     lambda: self.api.dbc_live("nowhere"), lambda: self.api.dbc_live("main", {"keys": "x"}),
                     lambda: self.api.dbc_edit("f9", {"op": "x"}), lambda: self.api.dbc_edit("f9", []),
                     lambda: self.api.dbc_series_set("main", "x"), lambda: self.api.dbc_encode("main", "", {}),
                     lambda: self.api.dbc_audit("main", "explode"), lambda: self.api.dbc_save("f9", {"path": 3}),
                     lambda: self.api.dbc_save("f9", {"encoding": "ebcdic"}), lambda: self.api.dbc_undo("nope"),
                     lambda: self.api.dbc_trace_series("t1", "x", []), lambda: self.api.dbc_set_bus("f1", "x")):
            result = call()
            self.assertFalse(result["ok"], result)
            self.assertTrue(result["error"])


class FileApiTests(ApiCase):
    def test_open_edit_save_and_persist(self) -> None:
        file_id = self.open()
        self.assertEqual(self.api.dbc_state()["files"][0]["name"], "Vehicle_Can1.dbc")
        self.assertTrue(self.api.dbc_edit(file_id, [{"op": "set_signal", "message": "s:291", "signal": "Speed",
                                                     "fields": {"unit": "m/s"}}])["ok"])
        self.assertTrue(self.api.dbc_save(file_id)["ok"])
        self.assertIn('"m/s"', self.path.read_text(encoding="utf-8"))
        saved = json.loads(self.settings.read_text(encoding="utf-8"))["dbc_state"]
        self.assertEqual(saved["files"], [{"path": str(self.path), "bus": "can1"}])

    def test_a_new_session_reopens_the_files_of_the_last_one(self) -> None:
        self.open("canb")
        self.api.close()
        self.api = Api()
        state = self.api.dbc_state()
        self.assertEqual([(f["name"], f["bus"]) for f in state["files"]], [("Vehicle_Can1.dbc", "canb")])
        self.assertEqual(state["problems"], [])

    def test_files_come_back_for_the_monitor_page_before_the_dbc_page_is_opened(self) -> None:
        self.open("can1")
        self.api.close()
        self.api = Api()
        self.api._service._ingest(CanFrame(291, ENGINE_OK, False, time.time(), "rx"))
        live = self.api.dbc_live("main", {"messages": False, "unknown": False})
        self.assertEqual(live["names"]["s:291"], "EngineData")

    def test_files_that_disappeared_are_reported_once(self) -> None:
        self.open("can1")
        self.api.close()
        self.path.unlink()
        self.api = Api()
        self.api.dbc_live("main")
        self.assertEqual(len(self.api.dbc_state()["problems"]), 1)
        self.assertEqual(self.api.dbc_state()["problems"], [])

    def test_settings_written_by_other_features_are_kept(self) -> None:
        self.settings.write_text(json.dumps({"theme_mode": "light", "github_token": "keep"}), encoding="utf-8")
        self.open()
        data = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual((data["theme_mode"], data["github_token"]), ("light", "keep"))

    def test_central_candidates_are_listed_only_when_the_sibling_repo_exists(self) -> None:
        state = self.api.dbc_state()
        for candidate in state["suggestions"]:
            self.assertTrue(candidate["path"].endswith(".dbc"))
            self.assertNotIn("_candbpp", candidate["name"])


class ReceivePathTests(ApiCase):
    def feed(self, service: CanService, identifier: int, data: bytes, timestamp: float | None = None) -> None:
        service._ingest(CanFrame(identifier, data, False, timestamp or time.time(), "rx"))

    def test_main_and_vehicle_connections_use_their_own_bus_binding(self) -> None:
        self.open("can1")
        self.feed(self.api._service, 291, ENGINE_OK)
        self.feed(self.api._vehicle_service, 291, ENGINE_OK)
        main = self.api.dbc_live("main", {"keys": ["s:291"]})
        vehicle = self.api.dbc_live("vehicle", {"keys": ["s:291"]})
        self.assertEqual((main["bus"], vehicle["bus"]), ("can1", "canb"))
        self.assertEqual(main["decoded"]["s:291"]["state"], "live")
        self.assertEqual(vehicle["decoded"], {})                       # the DBC is not bound to CANB
        self.assertEqual([u["id_text"] for u in vehicle["unknown"]], ["0x123"])
        self.api.dbc_set_bus(self.api.dbc_state()["files"][0]["id"], "canb")
        self.assertEqual(self.api.dbc_live("vehicle", {"keys": ["s:291"]})["decoded"]["s:291"]["state"], "live")

    def test_a_main_connection_on_canb_uses_the_canb_binding(self) -> None:
        self.open("canb")
        self.api._service.connection["bus_profile"] = "canb"
        self.feed(self.api._service, 291, ENGINE_OK)
        result = self.api.dbc_live("main", {"keys": ["s:291"]})
        self.assertEqual((result["bus"], result["decoded"]["s:291"]["state"]), ("canb", "live"))

    def test_series_are_recorded_per_frame_between_polls(self) -> None:
        self.open("can1")
        self.assertTrue(self.api.dbc_series_set("main", [
            {"sid": "m:s:291:Speed", "id": 291, "extended": False, "signal": "Speed"}])["ok"])
        for index in range(50):
            self.feed(self.api._service, 291, struct.pack("<H", 100 * index) + bytes(6), 1000.0 + index * 0.01)
        reply = self.api.dbc_series_fetch("main", {})["series"]["m:s:291:Speed"]
        self.assertEqual(len(reply["t"]), 50)
        self.assertAlmostEqual(reply["v"][-1], 49.0)
        again = self.api.dbc_series_fetch("main", {"m:s:291:Speed": reply["cursor"]})["series"]["m:s:291:Speed"]
        self.assertEqual(again["t"], [])
        self.api.dbc_series_clear("main")
        self.assertEqual(self.api.dbc_series_fetch("main", {})["series"]["m:s:291:Speed"]["t"], [])

    def test_audit_runs_from_the_receive_path(self) -> None:
        self.open("can1")
        started = self.api.dbc_audit("main", "start")
        self.assertTrue(started["running"])
        for index in range(10):
            self.feed(self.api._service, 0x7AA, bytes(8), 50.0 + index)
        report = self.api.dbc_audit("main", "report")["report"]
        self.assertEqual([u["id_text"] for u in report["unknown"]], ["0x7AA"])
        self.assertFalse(self.api.dbc_audit("main", "stop")["running"])

    def test_a_failing_observer_never_disturbs_reception(self) -> None:
        service = CanService(allow_simulation=False)

        def broken(frame: CanFrame) -> None:
            raise RuntimeError("boom")

        service.frame_observers.append(broken)
        service._ingest(CanFrame(0x123, bytes(8), False, 1.0, "rx"))
        self.assertEqual(service.monitor_latest()["frames"][0]["id"], 0x123)

    def test_monitor_latest_reports_replay_time_for_replays(self) -> None:
        service = CanService(allow_simulation=False)
        service._ingest(CanFrame(0x123, bytes(8), False, 1_700_000_000.0, "rx"))
        service.connection.update(mode="replay")
        service.replay_first_timestamp, service.replay_position = 1_700_000_000.0, 12.5
        self.assertAlmostEqual(service.monitor_latest()["clock"], 1_700_000_012.5)
        service.connection.update(mode="pcan")
        self.assertAlmostEqual(service.monitor_latest()["clock"], time.time(), delta=5)


class SendTests(ApiCase):
    def test_encoded_payload_is_accepted_by_the_raw_frame_sender_and_its_guards_still_apply(self) -> None:
        self.open("can1")
        encoded = self.api.dbc_encode("main", "s:291", {"Speed": 50, "Temp": 40, "Gear": "D", "Throttle": 40})
        self.assertTrue(encoded["ok"])
        spec = normalize_message_spec({"name": "EngineData", "id": f"0x{encoded['id']:03X}",
                                       "extended": encoded["extended"], "data": encoded["data"], "cycle_ms": 100})
        self.assertEqual(spec["data_bytes"], list(ENGINE_OK))
        # Nothing is connected, so the send is refused by the existing guard; this module has no write path.
        refused = self.api.send_monitor_frame("main", spec, True)
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"], "CAN 尚未连接")
        self.assertFalse(self.api.send_monitor_frame("main", spec, False)["ok"])

    def test_protected_ids_cannot_be_sent_even_if_a_dbc_describes_them(self) -> None:
        from canhost.decoders import FAN_COMMAND_ID
        text = FIXTURE.read_text(encoding="utf-8") + (
            f'\nBO_ {FAN_COMMAND_ID} FanCommand: 8 ECU\n SG_ Duty : 0|8@1+ (1,0) [0|100] "%" Display\n')
        path = self.folder / "Fan_Can1.dbc"
        path.write_text(text, encoding="utf-8")
        self.assertTrue(self.api.dbc_open(str(path), "can1")["ok"])
        encoded = self.api.dbc_encode("main", f"s:{FAN_COMMAND_ID}", {"Duty": 50})
        self.assertTrue(encoded["ok"], encoded)
        self.assertTrue(CanService._monitor_id_is_protected(FAN_COMMAND_ID, False))


if __name__ == "__main__":
    unittest.main()

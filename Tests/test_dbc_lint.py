"""Consistency checks, semantic diff and the catalog fields the editor relies on."""

from __future__ import annotations

from pathlib import Path
import unittest

from canhost.dbc.catalog import build_catalog, raw_range
from canhost.dbc.diff import count_ops, semantic_diff, text_diff
from canhost.dbc.document import DbcDocument
from canhost.dbc.lint import ERROR, NOTE, WARN, lint_catalog, summarize

FIXTURE = Path(__file__).parent / "fixtures" / "dbc" / "sample.dbc"


def database(*ops: dict) -> DbcDocument:
    document = DbcDocument.empty()
    document.apply({"op": "add_node", "name": "ECU"})
    document.apply({"op": "add_node", "name": "Display"})
    document.apply({"op": "add_message", "name": "Status", "id": 0x100, "extended": False, "dlc": 8,
                    "sender": "ECU", "cycle_time": 10})
    for op in ops:
        document.apply(op)
    return document


def signal(name: str, start: int, length: int, **fields) -> dict:
    return {"op": "add_signal", "message": "s:256", "fields": {
        "name": name, "start": start, "length": length, "receivers": ["Display"], "minimum": 0,
        "maximum": (1 << length) - 1, **fields}}


def findings(document: DbcDocument) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for issue in lint_catalog(build_catalog(document)):
        grouped.setdefault(issue["code"], []).append(issue)
    return grouped


class LintTests(unittest.TestCase):
    def test_a_clean_database_has_no_errors_or_warnings(self) -> None:
        result = findings(database(signal("A", 0, 8), signal("B", 8, 8)))
        self.assertEqual([code for code, items in result.items() if items[0]["level"] != NOTE], [])

    def test_overlapping_signals_are_errors_naming_both_and_the_bits(self) -> None:
        issue = findings(database(signal("A", 0, 8), signal("B", 4, 8)))["sig.overlap"][0]
        self.assertEqual(issue["level"], ERROR)
        self.assertIn("A", issue["message"])
        self.assertIn("B", issue["message"])
        self.assertIn("bit 4..7", issue["message"])

    def test_signals_selected_by_different_multiplexor_values_may_share_bits(self) -> None:
        document = database(
            signal("Sel", 0, 8, mux_role="multiplexor"),
            signal("X", 8, 16, mux_role="multiplexed", mux_value=0),
            signal("Y", 8, 16, mux_role="multiplexed", mux_value=1))
        self.assertNotIn("sig.overlap", findings(document))
        document.apply({"op": "set_signal", "message": "s:256", "signal": "Y", "fields": {"mux_value": 0}})
        self.assertIn("sig.overlap", findings(document))

    def test_extended_multiplexing_overlap_follows_the_value_ranges(self) -> None:
        def build(first: list, second: list) -> DbcDocument:
            return database(
                signal("Sel", 0, 8, mux_role="multiplexor"),
                signal("X", 8, 16, mux_role="multiplexed", mux_value=0, ext_mux={"signal": "Sel", "ranges": first}),
                signal("Y", 8, 16, mux_role="multiplexed", mux_value=2, ext_mux={"signal": "Sel", "ranges": second}))
        self.assertNotIn("sig.overlap", findings(build([[0, 1]], [[2, 3]])))
        self.assertIn("sig.overlap", findings(build([[0, 2]], [[2, 3]])))

    def test_out_of_dlc_float_length_factor_and_range_errors(self) -> None:
        document = database(
            signal("Wide", 60, 8),
            signal("Gain", 0, 8, factor=0),
            signal("Backwards", 8, 8, minimum=10, maximum=5),
            signal("Real", 16, 16, value_type="float32"))
        result = findings(document)
        self.assertEqual(result["sig.oob"][0]["signal"], "Wide")
        self.assertEqual(result["sig.factor.zero"][0]["signal"], "Gain")
        self.assertEqual(result["sig.minmax"][0]["signal"], "Backwards")
        self.assertEqual(result["sig.float.length"][0]["signal"], "Real")

    def test_value_table_codes_must_fit_the_signal(self) -> None:
        result = findings(database(signal("Mode", 0, 2, choices={"0": "Off", "5": "Impossible"})))
        self.assertEqual(result["sig.choice.range"][0]["signal"], "Mode")

    def test_physical_range_beyond_what_the_raw_bits_can_express_is_a_warning(self) -> None:
        result = findings(database(signal("T", 0, 8, factor=1, offset=-40, minimum=-40, maximum=300)))
        issue = result["sig.range.exceeds"][0]
        self.assertEqual(issue["level"], WARN)
        self.assertIn("215", issue["message"])

    def test_undeclared_nodes_orphan_multiplexed_signals_and_duplicates(self) -> None:
        text = ('VERSION ""\n\nBS_:\n\nBU_: ECU\n\n'
                'BO_ 256 Status: 8 Ghost\n'
                ' SG_ A m1 : 0|8@1+ (1,0) [0|255] "" Nobody\n'
                ' SG_ A : 8|8@1+ (1,0) [0|255] "" ECU\n\n'
                'BO_ 256 Other: 8 ECU\n SG_ B : 0|8@1+ (1,0) [0|255] "" ECU\n\n'
                'BO_ 257 Status: 8 ECU\n SG_ C : 0|8@1+ (1,0) [0|255] "" ECU\n')
        result = findings(DbcDocument.parse(text))
        self.assertIn("msg.sender.unknown", result)
        self.assertIn("sig.receiver.unknown", result)
        self.assertIn("sig.mux.orphan", result)
        self.assertIn("sig.name.dup", result)
        self.assertIn("msg.id.dup", result)
        self.assertIn("msg.name.dup", result)

    def test_illegal_names_are_errors(self) -> None:
        text = ('VERSION ""\n\nBS_:\n\nBU_: ECU\n\nBO_ 256 9Bad: 8 ECU\n'
                ' SG_ x-y : 0|8@1+ (1,0) [0|255] "" ECU\n')
        result = findings(DbcDocument.parse(text))
        self.assertEqual(len(result["name.invalid"]), 2)

    def test_chinese_text_gives_one_note_not_one_per_signal(self) -> None:
        document = database(signal("A", 0, 8, comment="温度"), signal("B", 8, 8, comment="电压"))
        notes = findings(document)["text.nonascii"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["level"], NOTE)
        self.assertIn("GBK", notes[0]["message"])

    def test_fd_length_unparsed_statements_and_summary(self) -> None:
        document = database()
        document.apply({"op": "set_message", "message": "s:256", "fields": {"dlc": 8}})
        document.items.append(DbcDocument.parse("BA_ broken;\n").items[0])
        document.warnings.append("无法解析的 BA_ 语句")
        counts = summarize(lint_catalog(build_catalog(document)))
        self.assertEqual(counts["errors"], 0)
        self.assertEqual(counts["warnings"], 1)

    def test_raw_range_covers_signed_unsigned_and_float(self) -> None:
        self.assertEqual(raw_range(8, False, "int"), (0, 255))
        self.assertEqual(raw_range(8, True, "int"), (-128, 127))
        self.assertGreater(raw_range(32, False, "float32")[1], 1e38)


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = build_catalog(DbcDocument.parse(FIXTURE.read_text(encoding="utf-8")))

    def message(self, name: str) -> dict:
        return next(m for m in self.catalog["messages"] if m["name"] == name)

    def test_messages_carry_ids_senders_cycle_and_comments(self) -> None:
        engine = self.message("EngineData")
        self.assertEqual((engine["key"], engine["id_text"], engine["cycle_ms"]), ("s:291", "0x123", 10))
        self.assertEqual(engine["senders"], ["ECU", "Gateway"])
        self.assertEqual(engine["comment"], "Engine data\nsecond line")
        extended = self.message("ExtStatus")
        self.assertEqual((extended["extended"], extended["id_text"]), (True, "0x1806E5F4"))
        self.assertTrue(self.message("VECTOR__INDEPENDENT_SIG_MSG")["virtual"])

    def test_signals_carry_layout_scaling_choices_and_long_names(self) -> None:
        speed = next(s for s in self.message("EngineData")["signals"] if s["name"] == "Speed")
        self.assertEqual((speed["start"], speed["length"], speed["byte_order"]), (0, 16, "intel"))
        self.assertEqual(speed["bits"], list(range(15, -1, -1)))
        self.assertEqual(speed["comment"], "车速")
        gear = next(s for s in self.message("EngineData")["signals"] if s["name"] == "Gear")
        self.assertEqual([c["label"] for c in gear["choices"]], ["P", "R", "N", "D"])
        torque = next(s for s in self.message("MotorolaMsg")["signals"] if s["name"] == "Torque")
        self.assertEqual((torque["byte_order"], torque["signed"]), ("motorola", True))
        self.assertEqual(torque["bits"][:2], [7, 6])
        names = [s["name"] for s in self.message("ExtStatus")["signals"]]
        self.assertIn("VeryLongSignalNameForTestingTheShortSymbol", names)
        long_signal = next(s for s in self.message("ExtStatus")["signals"] if s["name"].startswith("VeryLong"))
        self.assertEqual(len(long_signal["file_name"]), 32)
        ratio = next(s for s in self.message("FloatMsg")["signals"])
        self.assertEqual(ratio["value_type"], "float32")

    def test_multiplexing_nodes_and_attributes(self) -> None:
        roles = {s["name"]: s["mux"] for s in self.message("MuxMsg")["signals"]}
        self.assertEqual(roles["Sel"]["role"], "multiplexor")
        self.assertEqual((roles["Ana"]["role"], roles["Ana"]["value"]), ("multiplexed", 0))
        self.assertEqual({n["name"]: (n["sends"], n["receives"]) for n in self.catalog["nodes"]}["ECU"], (5, 1))
        attrs = {(a["scope"], a["name"]) for a in self.catalog["attr_defs"]}
        self.assertIn(("BO_", "GenMsgCycleTime"), attrs)

    def test_initial_value_is_reported_raw_and_physical(self) -> None:
        document = DbcDocument.parse(FIXTURE.read_text(encoding="utf-8"))
        document.apply({"op": "set_signal", "message": "s:291", "signal": "Temp", "fields": {"initial_raw": 80}})
        temp = next(s for s in build_catalog(document)["messages"][0]["signals"] if s["name"] == "Temp")
        self.assertEqual((temp["initial_raw"], temp["initial"]), (80, 40))


class DiffTests(unittest.TestCase):
    def test_changes_are_reported_per_object_and_field(self) -> None:
        before = DbcDocument.parse(FIXTURE.read_text(encoding="utf-8"))
        after = before.clone()
        for op in ({"op": "set_signal", "message": "s:291", "signal": "Speed", "fields": {"unit": "m/s"}},
                   {"op": "delete_signal", "message": "s:291", "signal": "Throttle"},
                   {"op": "add_node", "name": "Tester"},
                   {"op": "set_value_table", "name": "Flags", "choices": {"0": "Off"}},
                   {"op": "set_attr_def", "scope": "BO_", "name": "GenMsgDelay", "type": "INT", "args": "0 100",
                    "default": "0"}):
            after.apply(op)
        records = semantic_diff(build_catalog(before), build_catalog(after))
        by_target = {(r["scope"], r["op"], r["target"]) for r in records}
        self.assertIn(("signal", "change", "Speed"), by_target)
        self.assertIn(("signal", "delete", "Throttle"), by_target)
        self.assertIn(("node", "add", "Tester"), by_target)
        self.assertIn(("table", "add", "Flags"), by_target)
        self.assertIn(("attribute", "add", "GenMsgDelay"), by_target)
        speed = next(r for r in records if r["target"] == "Speed")
        self.assertEqual(speed["changes"], [{"field": "单位", "old": "km/h", "new": "m/s"}])
        self.assertEqual(count_ops(records), {"add": 3, "delete": 1, "change": 1})

    def test_identical_databases_have_no_differences_and_text_diff_is_bounded(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8")
        catalog = build_catalog(DbcDocument.parse(text))
        self.assertEqual(semantic_diff(catalog, catalog), [])
        diff = text_diff(text, text.replace("km/h", "m/s") + "\n" * 5, limit=3)
        self.assertTrue(diff["truncated"])
        self.assertEqual(len(diff["lines"]), 3)


if __name__ == "__main__":
    unittest.main()

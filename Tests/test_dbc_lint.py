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

    def test_invalid_numbers_standard_ids_and_initial_ranges(self) -> None:
        text = ('VERSION ""\n\nBS_:\n\nBU_: ECU\n\n'
                'BO_ 2048 Wide: 8 ECU\n'
                ' SG_ Bad : 0|8@1+ (1O,0) [0|255] "" ECU\n'
                'BA_DEF_ SG_ "GenSigStartValue" INT 0 64;\n'
                'BA_DEF_DEF_ "GenSigStartValue" 0;\n'
                'BA_ "GenSigStartValue" SG_ 2048 Bad 999;\n')
        result = findings(DbcDocument.parse(text))
        self.assertIn("sig.number", result)
        self.assertIn("msg.id.standard", result)
        self.assertIn("sig.initial.range", result)

    def test_extended_multiplexing_parent_must_be_a_selector(self) -> None:
        document = database(
            signal("Sel", 0, 8),
            signal("X", 8, 8, mux_role="multiplexed", mux_value=1,
                   ext_mux={"signal": "Sel", "ranges": [[1, 1]]}))
        self.assertIn("sig.mux.parent.role", findings(document))
        document.apply({"op": "set_signal", "message": "s:256", "signal": "Sel",
                        "fields": {"mux_role": "multiplexor"}})
        self.assertNotIn("sig.mux.parent.role", findings(document))

    def test_duplicate_attributes_and_tables_are_reported(self) -> None:
        text = ('VERSION ""\n\nBS_:\n\nBU_: ECU ECU\n\n'
                'BO_ 256 M: 8 ECU\n SG_ S : 0|8@1+ (1,0) [0|255] "" ECU\n\n'
                'VAL_TABLE_ T 0 "off" 0 "OFF" ;\nVAL_TABLE_ T 1 "on" ;\n'
                'BA_DEF_ BO_ "Kind" ENUM "A","B";\nBA_DEF_ BO_ "Kind" ENUM "A","B";\n'
                'BA_DEF_DEF_ "Kind" "A";\nBA_DEF_DEF_ "Kind" "B";\n'
                'BA_ "Kind" BO_ 256 0;\nBA_ "Kind" BO_ 256 1;\n'
                'BA_ "Ghost" BO_ 256 1;\n')
        result = findings(DbcDocument.parse(text))
        for code in ("node.name.dup", "table.name.dup", "table.choice.dup", "attr.def.dup",
                     "attr.default.dup", "attr.value.dup", "attr.value.undefined"):
            self.assertIn(code, result, code)

    def test_attribute_values_and_defaults_are_checked_against_their_definitions(self) -> None:
        text = ('VERSION ""\n\nBS_:\n\nBU_: ECU\n\n'
                'BO_ 256 M: 8 ECU\n SG_ S : 0|8@1+ (1,0) [0|255] "" ECU\n\n'
                'BA_DEF_ BO_ "Count" INT 0 10;\n'
                'BA_DEF_ BO_ "Free" INT 0 0;\n'
                'BA_DEF_ BO_ "Kind" ENUM "A","B";\n'
                'BA_DEF_ BO_ "Label" STRING ;\n'
                'BA_DEF_DEF_ "Count" "oops";\n'
                'BA_DEF_DEF_ "Kind" "C";\n'
                'BA_DEF_DEF_ "Label" 3;\n'
                'BA_ "Count" BO_ 256 11;\n'
                'BA_ "Free" BO_ 256 99999;\n'
                'BA_ "Kind" BO_ 256 5;\n')
        issues = findings(DbcDocument.parse(text))
        self.assertEqual([i["level"] for i in issues["attr.range"]], [WARN])
        self.assertIn("Count", issues["attr.range"][0]["message"])
        self.assertEqual(len(issues["attr.range"]), 1, "0 0 means no limit, so Free is fine")
        self.assertEqual([i["level"] for i in issues["attr.enum.range"]], [ERROR])
        self.assertEqual(len(issues["attr.default.type"]), 3)

    def test_non_numeric_attribute_value_is_an_error(self) -> None:
        text = ('VERSION ""\n\nBS_:\n\nBU_: ECU\n\n'
                'BO_ 256 M: 8 ECU\n SG_ S : 0|8@1+ (1,0) [0|255] "" ECU\n\n'
                'BA_DEF_ BO_ "Count" INT 0 10;\nBA_DEF_DEF_ "Count" 0;\n'
                'BA_ "Count" BO_ 256 "many";\n')
        self.assertEqual([i["level"] for i in findings(DbcDocument.parse(text))["attr.number"]], [ERROR])

    def test_extended_multiplexing_and_value_table_details_are_checked(self) -> None:
        text = ('VERSION ""\n\nBS_:\n\nBU_: ECU\n\n'
                'BO_ 256 M: 8 ECU\n'
                ' SG_ Sel M : 0|8@1+ (1,0) [0|255] "" ECU\n'
                ' SG_ Plain : 8|8@1+ (1,0) [0|255] "" ECU\n'
                ' SG_ X m1 : 16|8@1+ (1,0) [0|255] "" ECU\n\n'
                'VAL_ 256 X 1 "a" 1 "b" ;\n'
                'VAL_TABLE_ 9bad 0 "off" ;\n'
                'SIG_GROUP_ 256 G 1 : ;\n'
                'SG_MUL_VAL_ 256 Plain Sel 5-2;\n')
        issues = findings(DbcDocument.parse(text))
        self.assertIn("sig.mux.ext.role", issues)
        self.assertIn("sig.mux.range", issues)
        self.assertIn("sig.choice.dup", issues)
        self.assertIn("name.invalid", issues)

    def test_non_numeric_initial_value_is_an_error(self) -> None:
        text = ('VERSION ""\n\nBS_:\n\nBU_: ECU\n\n'
                'BO_ 256 M: 8 ECU\n SG_ S : 0|8@1+ (1,0) [0|255] "" ECU\n\n'
                'BA_DEF_ SG_ "GenSigStartValue" STRING ;\n'
                'BA_DEF_DEF_ "GenSigStartValue" "";\n'
                'BA_ "GenSigStartValue" SG_ 256 S "abc";\n')
        self.assertIn("sig.initial", findings(DbcDocument.parse(text)))

    def test_signal_group_references_are_checked(self) -> None:
        text = ('VERSION ""\n\nBS_:\n\nBU_: ECU\n\n'
                'BO_ 256 M: 8 ECU\n SG_ S : 0|8@1+ (1,0) [0|255] "" ECU\n\n'
                'SIG_GROUP_ 256 G 0 : S Missing;\nSIG_GROUP_ 256 G 1 : S;\n')
        result = findings(DbcDocument.parse(text))
        for code in ("group.repetitions", "group.signal", "group.name.dup"):
            self.assertIn(code, result, code)

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

    def test_large_initial_value_is_text_so_javascript_keeps_it_exact(self) -> None:
        raw = (1 << 64) - 1
        document = database(signal("Big", 0, 64, maximum=0))
        document.apply({"op": "set_signal", "message": "s:256", "signal": "Big", "fields": {"initial_raw": raw}})
        big = next(s for s in build_catalog(document)["messages"][0]["signals"] if s["name"] == "Big")
        self.assertEqual(big["initial_raw"], str(raw))

    def test_signal_groups_and_attribute_scopes_are_exposed(self) -> None:
        document = database(signal("A", 0, 8), signal("B", 8, 8))
        document.apply({"op": "set_signal_group", "message": "s:256", "name": "Pair",
                        "repetitions": 2, "signals": ["A", "B"]})
        document.apply({"op": "set_attr_def", "scope": "", "name": "Bus", "type": "STRING",
                        "args": "", "default": "CAN"})
        document.apply({"op": "set_attr_value", "scope": "", "name": "Bus", "value": "CANB"})
        document.apply({"op": "set_attr_def", "scope": "BO_", "name": "Mode", "type": "ENUM",
                        "args": '"Cycle","Event"', "default": "Cycle"})
        document.apply({"op": "set_attr_value", "scope": "BO_", "message": "s:256",
                        "name": "Mode", "value": "Event"})
        document.apply({"op": "set_attr_def", "scope": "BU_", "name": "Role", "type": "STRING",
                        "args": "", "default": ""})
        document.apply({"op": "set_attr_value", "scope": "BU_", "node": "ECU", "name": "Role", "value": "master"})
        document.apply({"op": "set_attr_def", "scope": "SG_", "name": "UnitKind", "type": "STRING",
                        "args": "", "default": ""})
        document.apply({"op": "set_attr_value", "scope": "SG_", "message": "s:256", "signal": "A",
                        "name": "UnitKind", "value": "raw"})
        catalog = build_catalog(document)
        message = catalog["messages"][0]
        self.assertEqual(message["signal_groups"], [{"name": "Pair", "repetitions": 2, "signals": ["A", "B"]}])
        self.assertEqual(message["attrs"], {"Mode": 1})
        self.assertEqual(catalog["attrs"], {"Bus": "CANB"})
        node = next(n for n in catalog["nodes"] if n["name"] == "ECU")
        self.assertEqual(node["attrs"], {"Role": "master"})
        signal_a = next(s for s in message["signals"] if s["name"] == "A")
        self.assertEqual(signal_a["attrs"], {"UnitKind": "raw"})
        mode = next(a for a in catalog["attr_defs"] if a["name"] == "Mode")
        self.assertEqual(mode["choices"], ["Cycle", "Event"])


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

    def test_attribute_values_and_signal_groups_appear_in_the_review(self) -> None:
        before = DbcDocument.parse(FIXTURE.read_text(encoding="utf-8"))
        after = before.clone()
        after.apply({"op": "set_attr_def", "scope": "BO_", "name": "Mode", "type": "ENUM",
                     "args": '"Cycle","Event"', "default": "Cycle"})
        after.apply({"op": "set_attr_value", "scope": "BO_", "message": "s:291", "name": "Mode", "value": "Event"})
        after.apply({"op": "set_signal_group", "message": "s:291", "name": "Pair",
                     "repetitions": 1, "signals": ["Speed", "Gear"]})
        records = semantic_diff(build_catalog(before), build_catalog(after))
        engine = next(r for r in records if r["scope"] == "message" and r["target"] == "EngineData")
        fields = {change["field"]: (change["old"], change["new"]) for change in engine["changes"]}
        self.assertEqual(fields["属性"], ("—", "Mode=Event(1)"))
        self.assertEqual(fields["信号组"], ("—", "Pair(×1): Speed Gear"))

    def test_changing_only_an_attribute_default_lists_just_the_default(self) -> None:
        before = DbcDocument.parse(FIXTURE.read_text(encoding="utf-8"))
        before.apply({"op": "set_attr_def", "scope": "BO_", "name": "Mode", "type": "ENUM",
                      "args": '"Cycle","Event"', "default": "Cycle"})
        after = before.clone()
        after.apply({"op": "set_attr_def", "scope": "BO_", "name": "Mode", "type": "ENUM",
                     "args": '"Cycle","Event"', "default": "Event"})
        records = semantic_diff(build_catalog(before), build_catalog(after))
        changed = next(r for r in records if r["scope"] == "attribute" and r["target"] == "Mode")
        self.assertEqual(changed["changes"], [{"field": "默认值", "old": "Cycle", "new": "Event"}])

    def test_identical_databases_have_no_differences_and_text_diff_is_bounded(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8")
        catalog = build_catalog(DbcDocument.parse(text))
        self.assertEqual(semantic_diff(catalog, catalog), [])
        diff = text_diff(text, text.replace("km/h", "m/s") + "\n" * 5, limit=3)
        self.assertTrue(diff["truncated"])
        self.assertEqual(len(diff["lines"]), 3)


if __name__ == "__main__":
    unittest.main()

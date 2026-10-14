"""The DBC document model: lossless reading, targeted edits, CANdb++ text."""

from __future__ import annotations

import difflib
from pathlib import Path
import unittest

import cantools

from canhost.dbc import codec
from canhost.dbc.catalog import build_catalog
from canhost.dbc.document import (
    DbcDocument, DbcEditError, escape_text, format_number, message_key, parse_message_key, unescape_text,
)

FIXTURE = Path(__file__).parent / "fixtures" / "dbc" / "sample.dbc"
CENTRAL = Path(__file__).resolve().parents[2] / "vehicle-interfaces" / "can"
LONG_NAME = "VeryLongSignalNameForTestingTheShortSymbol"
EXTENDED_KEY = message_key(2550588916)


def fixture_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def changed_lines(before: str, after: str) -> list[str]:
    return [line for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
            if line[:1] in "+-" and not line.startswith(("+++", "---"))]


def signal_of(doc: DbcDocument, message: str, signal: str):
    message_item = doc.find_message_by_name(message)
    return doc.find_signal(message_item, signal)


class RoundTripTests(unittest.TestCase):
    def test_fixture_is_reproduced_byte_for_byte(self) -> None:
        text = fixture_text()
        document = DbcDocument.parse(text)
        self.assertEqual(document.to_text(), text)
        self.assertEqual(document.warnings, [])

    def test_crlf_and_mixed_line_ends_are_kept(self) -> None:
        crlf = fixture_text().replace("\n", "\r\n")
        self.assertEqual(DbcDocument.parse(crlf).to_text(), crlf)
        mixed = fixture_text().replace("\n", "\r\n", 6)
        self.assertEqual(DbcDocument.parse(mixed).to_text(), mixed)

    def test_untouched_statements_survive_unusual_spacing(self) -> None:
        odd = ('VERSION ""\n\nBS_:\n\nBU_: A B\n\nBO_ 5 M: 8 A\n'
               ' SG_ S : 0|8@1+ ( 1 , 0 ) [ 0 | 255 ] "u"   B ,A\n'
               '\n// stray comment\nCM_ SG_ 5 S "a \\"quoted\\" word";\n')
        document = DbcDocument.parse(odd)
        self.assertEqual(document.to_text(), odd)
        self.assertEqual(document.comment_of("SG_", 5, "S"), 'a "quoted" word')

    def test_dummy_vector_message_is_kept_and_flagged(self) -> None:
        document = DbcDocument.parse(fixture_text())
        dummy = [m for m in document.messages() if m.virtual]
        self.assertEqual(len(dummy), 1)
        self.assertIn("VECTOR__INDEPENDENT_SIG_MSG", document.to_text())
        self.assertEqual(message_key(dummy[0].raw_id), "v")

    def test_multi_line_comment_text_and_unparsable_statement(self) -> None:
        document = DbcDocument.parse(fixture_text())
        engine = document.find_message_by_name("EngineData")
        self.assertEqual(document.comment_of("BO_", engine.raw_id), "Engine data\nsecond line")
        broken = DbcDocument.parse(fixture_text() + "BA_ broken statement;\n")
        self.assertEqual(len(broken.warnings), 1)
        self.assertTrue(broken.to_text().endswith("BA_ broken statement;\n"))

    def test_escape_helpers_round_trip(self) -> None:
        for text in ('plain', 'say "hi"', 'back\\slash', 'trailing\\', 'a\\"b', '\\n literal'):
            self.assertEqual(unescape_text(escape_text(text)), text)

    def test_number_formatting_is_short_and_exact(self) -> None:
        self.assertEqual(format_number(1.0), "1")
        self.assertEqual(format_number(0.1), "0.1")
        self.assertEqual(format_number(-40), "-40")
        self.assertEqual(float(format_number(1e-05)), 1e-05)
        with self.assertRaises(DbcEditError):
            format_number(float("nan"))

    def test_message_keys_round_trip(self) -> None:
        self.assertEqual(parse_message_key("s:291"), 291)
        self.assertEqual(parse_message_key("e:100"), 100 | 0x80000000)
        self.assertEqual(message_key(100 | 0x80000000), "e:100")
        with self.assertRaises(DbcEditError):
            parse_message_key("x:1")

    @unittest.skipUnless(CENTRAL.is_dir(), "兄弟仓库 vehicle-interfaces 不存在")
    def test_every_team_dbc_round_trips_and_matches_cantools(self) -> None:
        files = sorted(CENTRAL.glob("*.dbc"))
        self.assertTrue(files)
        for path in files:
            with self.subTest(path.name):
                text, _encoding = codec.read_file(path)
                document = DbcDocument.parse(text)
                self.assertEqual(document.to_text(), text)
                self.assertEqual(document.warnings, [])
                database = cantools.database.load_string(text, database_format="dbc", strict=True)
                catalog = build_catalog(document)
                real = [m for m in catalog["messages"] if not m["virtual"]]
                self.assertEqual(len(real), len(database.messages))
                for entry in real:
                    message = database.get_message_by_name(entry["name"])
                    self.assertEqual(message.length, entry["dlc"])
                    self.assertEqual({s.name for s in message.signals}, {s["name"] for s in entry["signals"]})
                    for signal in entry["signals"]:
                        other = message.get_signal_by_name(signal["name"])
                        self.assertEqual((other.start, other.length, other.scale, other.offset),
                                         (signal["start"], signal["length"], signal["factor"], signal["offset"]))
                        self.assertEqual(other.byte_order == "little_endian", signal["byte_order"] == "intel")


class EditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = fixture_text()
        self.document = DbcDocument.parse(self.text)

    def apply(self, op: dict) -> str:
        self.document.apply(op)
        return self.document.to_text()

    def test_changing_one_field_rewrites_only_that_line(self) -> None:
        after = self.apply({"op": "set_signal", "message": "s:291", "signal": "Speed",
                            "fields": {"unit": "m/s"}})
        lines = changed_lines(self.text, after)
        self.assertEqual(len(lines), 2, lines)
        self.assertIn('"m/s"', lines[1])

    def test_renaming_a_signal_updates_every_reference(self) -> None:
        after = self.apply({"op": "set_signal", "message": "s:291", "signal": "Gear",
                            "fields": {"name": "GearState"}})
        self.assertIn("SG_ GearState :", after)
        self.assertIn("VAL_ 291 GearState 0", after)
        self.assertNotIn(" Gear ", after.replace("GearState", ""))
        cantools.database.load_string(after, database_format="dbc", strict=True)

    def test_renaming_the_comment_target_and_signal_value_type(self) -> None:
        after = self.apply({"op": "set_signal", "message": "s:291", "signal": "Speed",
                            "fields": {"name": "VehicleSpeed"}})
        self.assertIn('CM_ SG_ 291 VehicleSpeed "车速";', after)
        after = self.apply({"op": "set_signal", "message": "s:1024", "signal": "Ratio",
                            "fields": {"name": "Gain"}})
        self.assertIn("SIG_VALTYPE_ 1024 Gain : 1;", after)

    def test_changing_an_id_moves_comments_values_and_attributes(self) -> None:
        after = self.apply({"op": "set_message", "message": "s:291", "fields": {"id": 600}})
        self.assertIn("BO_ 600 EngineData: 8 ECU", after)
        self.assertIn('CM_ BO_ 600 "Engine data', after)
        self.assertIn("VAL_ 600 Gear", after)
        self.assertIn('BA_ "GenMsgCycleTime" BO_ 600 10;', after)
        self.assertIn("BO_TX_BU_ 600 :", after)
        self.assertNotIn(" 291 ", after)
        cantools.database.load_string(after, database_format="dbc", strict=True)

    def test_id_collisions_and_range_errors_are_rejected(self) -> None:
        for fields in ({"id": 512}, {"id": 0x800}, {"id": -1}):
            with self.assertRaises(DbcEditError):
                self.document.apply({"op": "set_message", "message": "s:291", "fields": fields})
        self.assertEqual(self.document.to_text(), self.text)

    def test_a_failed_edit_leaves_the_document_untouched(self) -> None:
        with self.assertRaises(DbcEditError):
            self.document.apply({"op": "set_signal", "message": "s:291", "signal": "Speed",
                                 "fields": {"unit": "x", "name": "9bad"}})
        self.assertEqual(self.document.to_text(), self.text)
        with self.assertRaises(DbcEditError):
            self.document.apply({"op": "set_choices", "message": "s:291", "signal": "Gear",
                                 "choices": {"1": "a", "0x1": "b"}})
        self.assertEqual(self.document.to_text(), self.text)
        with self.assertRaises(DbcEditError):
            self.document.apply({"op": "nonsense"})

    def test_adding_a_message_with_cycle_time_and_comment(self) -> None:
        after = self.apply({"op": "add_message", "name": "NewMsg", "id": 1000, "extended": False, "dlc": 4,
                            "sender": "ECU", "cycle_time": 50, "comment": "测试报文"})
        self.assertIn("BO_ 1000 NewMsg: 4 ECU", after)
        self.assertIn('BA_ "GenMsgCycleTime" BO_ 1000 50;', after)
        self.assertIn('CM_ BO_ 1000 "测试报文";', after)
        database = cantools.database.load_string(after, database_format="dbc", strict=True)
        self.assertEqual(database.get_message_by_name("NewMsg").cycle_time, 50)

    def test_adding_a_signal_and_choices(self) -> None:
        self.apply({"op": "add_signal", "message": "s:291", "fields": {
            "name": "Brake", "start": 40, "length": 2, "factor": 1, "offset": 0, "minimum": 0, "maximum": 3,
            "receivers": ["Display"], "comment": "制动", "choices": {"0": "Off", "1": "On"}}})
        text = self.document.to_text()
        self.assertIn(" SG_ Brake : 40|2@1+ (1,0) [0|3] \"\" Display", text)
        self.assertIn('VAL_ 291 Brake 0 "Off" 1 "On" ;', text)
        database = cantools.database.load_string(text, database_format="dbc", strict=True)
        self.assertEqual(database.get_message_by_name("EngineData").get_signal_by_name("Brake").length, 2)

    def test_duplicate_signal_name_is_rejected(self) -> None:
        with self.assertRaises(DbcEditError):
            self.document.apply({"op": "add_signal", "message": "s:291", "fields": {"name": "Speed"}})

    def test_deleting_a_message_removes_all_of_its_statements(self) -> None:
        after = self.apply({"op": "delete_message", "message": "s:291"})
        for fragment in ("EngineData", "VAL_ 291", "CM_ SG_ 291", "GenMsgCycleTime\" BO_ 291", "BO_TX_BU_ 291"):
            self.assertNotIn(fragment, after)
        self.assertNotIn("\n\n\n\n", after)
        cantools.database.load_string(after, database_format="dbc", strict=True)

    def test_deleting_a_signal_removes_its_references_only(self) -> None:
        after = self.apply({"op": "delete_signal", "message": "s:291", "signal": "Gear"})
        self.assertNotIn("VAL_ 291", after)
        self.assertIn("SG_ Speed", after)

    def test_duplicate_message_copies_signals_and_references(self) -> None:
        after = self.apply({"op": "duplicate_message", "message": "s:291", "name": "EngineCopy", "id": 292,
                            "extended": False})
        database = cantools.database.load_string(after, database_format="dbc", strict=True)
        copy = database.get_message_by_name("EngineCopy")
        self.assertEqual(copy.frame_id, 292)
        self.assertEqual([s.name for s in copy.signals], [s.name for s in database.get_message_by_name("EngineData").signals])
        self.assertEqual(copy.get_signal_by_name("Gear").choices[3], "D")
        self.assertEqual(copy.comment, "Engine data\nsecond line")

    def test_duplicate_signal_copies_value_table_and_comment(self) -> None:
        after = self.apply({"op": "duplicate_signal", "message": "s:291", "signal": "Gear", "name": "Gear2"})
        self.assertIn('VAL_ 291 Gear2 0 "P"', after)
        # The copy sits on the same bits until it is moved; strict parsing says so.
        with self.assertRaises(Exception):
            cantools.database.load_string(after, database_format="dbc", strict=True)
        self.apply({"op": "set_signal", "message": "s:291", "signal": "Gear2", "fields": {"start": 48}})
        cantools.database.load_string(self.document.to_text(), database_format="dbc", strict=True)

    def test_move_message_and_signal_change_order_only(self) -> None:
        self.apply({"op": "move_message", "message": "s:1288", "index": 0})
        self.assertEqual([m.name for m in self.document.messages()][:2], ["Reversed", "EngineData"])
        self.apply({"op": "move_signal", "message": "s:291", "signal": "Throttle", "index": 0})
        message = self.document.find_message("s:291")
        self.assertEqual(self.document.signals(message)[0].name, "Throttle")
        cantools.database.load_string(self.document.to_text(), database_format="dbc", strict=True)

    def test_node_rename_and_delete(self) -> None:
        after = self.apply({"op": "rename_node", "node": "Display", "name": "Cluster"})
        self.assertIn("BU_: ECU Cluster Gateway", after)
        self.assertIn('"km/h" Cluster,Gateway', after)
        with self.assertRaises(DbcEditError):
            self.document.apply({"op": "delete_node", "node": "ECU"})
        self.apply({"op": "delete_node", "node": "Cluster"})
        self.assertNotIn("Cluster", self.document.to_text())

    def test_cycle_time_attribute_default_and_removal(self) -> None:
        after = self.apply({"op": "set_message", "message": "s:1288", "fields": {"cycle_time": 100}})
        self.assertIn('BA_ "GenMsgCycleTime" BO_ 1288 100;', after)
        message = self.document.find_message("s:1288")
        self.assertEqual(self.document.message_cycle_time(message), 100)
        self.apply({"op": "set_message", "message": "s:1288", "fields": {"cycle_time": None}})
        self.assertIsNone(self.document.message_cycle_time(message))

    def test_attribute_definition_is_created_on_demand(self) -> None:
        document = DbcDocument.parse('VERSION ""\n\nBS_:\n\nBU_: A\n\nBO_ 5 M: 8 A\n SG_ S : 0|8@1+ (1,0) [0|0] "" A\n')
        document.apply({"op": "set_message", "message": "s:5", "fields": {"cycle_time": 20}})
        text = document.to_text()
        self.assertIn('BA_DEF_ BO_ "GenMsgCycleTime" INT 0 65535;', text)
        self.assertIn('BA_DEF_DEF_ "GenMsgCycleTime" 0;', text)
        self.assertEqual(cantools.database.load_string(text, database_format="dbc").messages[0].cycle_time, 20)

    def test_long_names_use_the_short_symbol_convention(self) -> None:
        long_name = "ThisIsAMuchLongerSignalNameThanThirtyTwoCharacters"
        self.apply({"op": "add_signal", "message": "s:291", "fields": {"name": long_name, "start": 40, "length": 4}})
        text = self.document.to_text()
        self.assertIn(f" SG_ {long_name[:32]} :", text)
        self.assertIn(f'"SystemSignalLongSymbol" SG_ 291 {long_name[:32]} "{long_name}";', text)
        database = cantools.database.load_string(text, database_format="dbc", strict=True)
        self.assertEqual(database.get_message_by_name("EngineData").get_signal_by_name(long_name).length, 4)
        self.apply({"op": "set_signal", "message": "s:291", "signal": long_name, "fields": {"name": "Short"}})
        self.assertNotIn("SystemSignalLongSymbol\" SG_ 291", self.document.to_text())

    def test_existing_long_names_resolve_by_either_name(self) -> None:
        message = self.document.find_message(EXTENDED_KEY)
        self.assertEqual(self.document.signal_display(message, self.document.signals(message)[1]), LONG_NAME)
        self.apply({"op": "set_signal", "message": EXTENDED_KEY, "signal": LONG_NAME, "fields": {"unit": "A"}})
        self.assertIn('"A" ECU', self.document.to_text())

    def test_multiplexing_and_float_type_fields(self) -> None:
        self.apply({"op": "set_signal", "message": "s:768", "signal": "Common",
                    "fields": {"mux_role": "multiplexed", "mux_value": 2}})
        self.assertIn(" SG_ Common m2 : ", self.document.to_text())
        self.apply({"op": "set_signal", "message": "s:768", "signal": "Common", "fields": {"mux_role": "none"}})
        self.apply({"op": "set_signal", "message": "s:1024", "signal": "Ratio", "fields": {"value_type": "float64",
                                                                                           "length": 64}})
        self.assertIn("SIG_VALTYPE_ 1024 Ratio : 2;", self.document.to_text())
        self.apply({"op": "set_signal", "message": "s:1024", "signal": "Ratio", "fields": {"value_type": "int",
                                                                                           "length": 8}})
        self.assertNotIn("SIG_VALTYPE_ 1024", self.document.to_text())

    def test_extended_multiplexing_statement(self) -> None:
        self.apply({"op": "set_signal", "message": "s:768", "signal": "Cur",
                    "fields": {"mux_role": "multiplexed", "mux_value": 1,
                               "ext_mux": {"signal": "Sel", "ranges": [[1, 3]]}}})
        self.assertIn("SG_MUL_VAL_ 768 Cur Sel 1-3;", self.document.to_text())
        self.apply({"op": "set_signal", "message": "s:768", "signal": "Cur", "fields": {"ext_mux": None}})
        self.assertNotIn("SG_MUL_VAL_ 768", self.document.to_text())

    def test_comments_value_tables_and_attributes(self) -> None:
        self.apply({"op": "set_comment", "scope": "signal", "message": "s:291", "signal": "Temp",
                    "comment": "冷却液温度"})
        self.assertIn('CM_ SG_ 291 Temp "冷却液温度";', self.document.to_text())
        self.apply({"op": "set_comment", "scope": "signal", "message": "s:291", "signal": "Temp", "comment": ""})
        self.assertNotIn("CM_ SG_ 291 Temp", self.document.to_text())
        self.apply({"op": "set_value_table", "name": "OnOff", "choices": {"0": "Off", "1": "On"}})
        self.assertIn('VAL_TABLE_ OnOff 0 "Off" 1 "On" ;', self.document.to_text())
        self.apply({"op": "set_attr_def", "scope": "BO_", "name": "GenMsgDelayTime", "type": "INT",
                    "args": "0 1000", "default": "0"})
        self.apply({"op": "set_attr_value", "scope": "BO_", "message": "s:291", "name": "GenMsgDelayTime",
                    "value": 5})
        self.assertIn('BA_ "GenMsgDelayTime" BO_ 291 5;', self.document.to_text())
        with self.assertRaises(DbcEditError):
            self.document.apply({"op": "set_attr_value", "scope": "BO_", "message": "s:291",
                                 "name": "Undefined", "value": 1})

    def test_initial_value_is_stored_raw_in_the_start_value_attribute(self) -> None:
        self.apply({"op": "set_signal", "message": "s:291", "signal": "Temp", "fields": {"initial_raw": 80}})
        database = cantools.database.load_string(self.document.to_text(), database_format="dbc", strict=True)
        signal = database.get_message_by_name("EngineData").get_signal_by_name("Temp")
        self.assertEqual(signal.raw_initial, 80)
        self.assertEqual(signal.initial, 40)

    def test_new_database_can_be_built_from_scratch(self) -> None:
        document = DbcDocument.empty()
        document.apply({"op": "add_node", "name": "ECU"})
        document.apply({"op": "add_message", "name": "Status", "id": 0x100, "extended": False, "dlc": 8,
                        "sender": "ECU"})
        document.apply({"op": "add_signal", "message": "s:256", "fields": {
            "name": "Rpm", "start": 0, "length": 16, "factor": 0.25, "unit": "rpm", "maximum": 16383.75}})
        database = cantools.database.load_string(document.to_text(), database_format="dbc", strict=True)
        self.assertEqual(database.get_message_by_name("Status").get_signal_by_name("Rpm").scale, 0.25)


class EdgeCaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = fixture_text()
        self.document = DbcDocument.parse(self.text)

    def test_a_failure_after_part_of_an_edit_ran_restores_everything(self) -> None:
        with self.assertRaises(DbcEditError):
            self.document.apply({"op": "set_signal", "message": "s:291", "signal": "Gear",
                                 "fields": {"name": "Renamed", "unit": "x", "receivers": ["Ghost"]}})
        self.assertEqual(self.document.to_text(), self.text)
        with self.assertRaises(DbcEditError):
            self.document.apply({"op": "set_message", "message": "s:291",
                                 "fields": {"id": 700, "name": "Other", "senders": ["Ghost"]}})
        self.assertEqual(self.document.to_text(), self.text)
        with self.assertRaises(DbcEditError):
            self.document.apply({"op": "add_node", "name": "Display"})
        self.assertEqual(self.document.to_text(), self.text)

    def test_switching_a_message_between_standard_and_extended_moves_its_references(self) -> None:
        self.document.apply({"op": "set_message", "message": "s:291", "fields": {"extended": True}})
        text = self.document.to_text()
        self.assertIn(f"BO_ {291 | 0x80000000} EngineData: 8 ECU", text)
        self.assertIn(f"VAL_ {291 | 0x80000000} Gear", text)
        database = cantools.database.load_string(text, database_format="dbc", strict=True)
        self.assertTrue(database.get_message_by_name("EngineData").is_extended_frame)

    def test_the_vector_dummy_message_can_be_deleted_but_not_edited(self) -> None:
        with self.assertRaises(DbcEditError):
            self.document.apply({"op": "set_message", "message": "v", "fields": {"dlc": 4}})
        self.document.apply({"op": "delete_message", "message": "v"})
        self.assertNotIn("VECTOR__INDEPENDENT_SIG_MSG", self.document.to_text())
        self.assertNotIn("Orphan", self.document.to_text())

    def test_renaming_a_message_and_a_node_to_long_names_and_back(self) -> None:
        long_name = "EngineDataWithAVeryLongDescriptiveNameForTheCluster"
        self.document.apply({"op": "set_message", "message": "s:291", "fields": {"name": long_name}})
        text = self.document.to_text()
        self.assertIn(f'"SystemMessageLongSymbol" BO_ 291 "{long_name}";', text)
        database = cantools.database.load_string(text, database_format="dbc", strict=True)
        self.assertEqual(database.get_message_by_name(long_name).frame_id, 291)
        self.document.apply({"op": "set_message", "message": "s:291", "fields": {"name": "EngineData"}})
        self.assertNotIn("SystemMessageLongSymbol\" BO_ 291", self.document.to_text())
        self.document.apply({"op": "rename_node", "node": "Display", "name": "InstrumentClusterDisplayUnitNumberOne"})
        text = self.document.to_text()
        self.assertIn("SystemNodeLongSymbol", text)
        self.assertIn("InstrumentClusterDisplayUnitNumberOn", text)       # the 32-character form
        node = next(n for n in build_catalog(self.document)["nodes"] if n["file_name"].startswith("InstrumentCluster"))
        self.assertEqual(node["name"], "InstrumentClusterDisplayUnitNumberOne")

    def test_two_long_names_with_the_same_prefix_get_distinct_file_names(self) -> None:
        prefix = "S" * 33
        self.document.apply({"op": "add_signal", "message": "s:291", "fields": {"name": prefix + "A", "start": 40, "length": 1}})
        self.document.apply({"op": "add_signal", "message": "s:291", "fields": {"name": prefix + "B", "start": 41, "length": 1}})
        names = [s["file_name"] for s in build_catalog(self.document)["messages"][0]["signals"][-2:]]
        self.assertEqual(len(set(names)), 2)
        self.assertTrue(all(len(name) <= 32 for name in names))
        cantools.database.load_string(self.document.to_text(), database_format="dbc", strict=True)

    def test_moving_messages_to_both_ends_keeps_blocks_whole(self) -> None:
        self.document.apply({"op": "move_message", "message": "s:291", "index": 99})
        names = [m.name for m in self.document.messages()]
        self.assertEqual(names[-1], "EngineData")
        self.document.apply({"op": "move_message", "message": "s:291", "index": 0})
        self.assertEqual([m.name for m in self.document.messages()][0], "EngineData")
        text = self.document.to_text()
        self.assertNotIn("\n\n\n\n", text)
        cantools.database.load_string(text, database_format="dbc", strict=True)

    def test_attributes_for_nodes_and_signals_and_string_values(self) -> None:
        self.document.apply({"op": "set_attr_def", "scope": "BU_", "name": "NodeKind", "type": "STRING", "args": "", "default": '""'})
        self.document.apply({"op": "set_attr_value", "scope": "BU_", "node": "ECU", "name": "NodeKind", "value": "main"})
        self.assertIn('BA_ "NodeKind" BU_ ECU "main";', self.document.to_text())
        self.document.apply({"op": "set_attr_def", "scope": "BO_", "name": "SendMode", "type": "ENUM", "args": '"Cycle","Event"', "default": '"Cycle"'})
        with self.assertRaises(DbcEditError):
            self.document.apply({"op": "set_attr_value", "scope": "BO_", "message": "s:291", "name": "SendMode", "value": "Never"})
        self.document.apply({"op": "set_attr_value", "scope": "BO_", "message": "s:291", "name": "SendMode", "value": "Event"})
        self.assertIn('BA_ "SendMode" BO_ 291 "Event";', self.document.to_text())
        self.document.apply({"op": "delete_attr_def", "scope": "BO_", "name": "SendMode"})
        self.assertNotIn("SendMode", self.document.to_text())

    def test_unusual_but_valid_inputs_parse(self) -> None:
        text = ('VERSION "a \\"quoted\\" version"\r\n\r\nBS_:\r\n\r\nBU_:\r\n\r\n'
                'BO_ 5 Lonely: 0 Vector__XXX\r\n\r\n'
                'CM_ "global \\"comment\\"";\r\n'
                'BA_ "BusType" "CAN";\r\n'
                'VAL_TABLE_ Empty ;\r\n')
        document = DbcDocument.parse(text)
        self.assertEqual(document.to_text(), text)
        self.assertEqual(document.warnings, [])
        catalog = build_catalog(document)
        self.assertEqual(catalog["version"], 'a "quoted" version')
        self.assertEqual(catalog["comment"], 'global "comment"')
        self.assertEqual(catalog["messages"][0]["dlc"], 0)


class CandbppTests(unittest.TestCase):
    def test_sections_are_ordered_and_comments_flattened(self) -> None:
        document = DbcDocument.parse(fixture_text())
        text = document.candbpp_text()
        self.assertTrue(text.endswith("\r\n") and "\n" not in text.replace("\r\n", ""))
        self.assertLess(text.index("BO_ 2550588916"), text.index("CM_ BU_"))
        self.assertLess(text.index("CM_ BU_"), text.index("BA_DEF_ "))
        self.assertLess(text.index("BA_DEF_ "), text.index("BA_ \"GenMsgCycleTime\" BO_ 291"))
        self.assertLess(text.index("BA_ \"GenMsgCycleTime\" BO_ 291"), text.index("VAL_ 291"))
        self.assertLess(text.index("VAL_ 291"), text.index("SIG_VALTYPE_ 1024"))
        self.assertIn('CM_ BO_ 291 "Engine data second line";', text)
        self.assertIn('VAL_ 291 Gear 0 "P" 1 "R" 2 "N" 3 "D" ;', text)
        self.assertIn("SIG_VALTYPE_ 1024 Ratio : 1 ;", text)
        cantools.database.load_string(text, database_format="dbc", strict=True)

    def test_candbpp_text_keeps_the_semantics(self) -> None:
        document = DbcDocument.parse(fixture_text())
        before = build_catalog(document)
        after = build_catalog(DbcDocument.parse(document.candbpp_text()))
        self.assertEqual([m["name"] for m in before["messages"]], [m["name"] for m in after["messages"]])
        for a, b in zip(before["messages"], after["messages"]):
            self.assertEqual([(s["name"], s["start"], s["length"], s["choices"]) for s in a["signals"]],
                             [(s["name"], s["start"], s["length"], s["choices"]) for s in b["signals"]])

    @unittest.skipUnless(CENTRAL.is_dir(), "兄弟仓库 vehicle-interfaces 不存在")
    def test_matches_the_team_candbpp_files(self) -> None:
        """Three of the four team files are byte-identical to what the team's
        normalizer produced; the fourth was edited by hand afterwards."""
        matches = 0
        for stem in ("Vehicle_Can1", "Vehicle_CanA", "Vehicle_CanC"):
            source, _ = codec.read_file(CENTRAL / f"{stem}.dbc")
            golden, _ = codec.read_file(CENTRAL / f"{stem}_candbpp_gbk.dbc")
            matches += DbcDocument.parse(source).candbpp_text() == golden
        self.assertEqual(matches, 3)


class CodecTests(unittest.TestCase):
    def test_encoding_detection_and_round_trip(self) -> None:
        text = "CM_ \"中文注释\";\n"
        for encoding in ("utf-8", "utf-8-sig", "gbk"):
            raw = codec.encode_text(text, encoding)
            decoded, found = codec.decode_bytes(raw)
            self.assertEqual(decoded, text)
            self.assertEqual(found, encoding)

    def test_unencodable_characters_are_an_error_not_a_replacement(self) -> None:
        with self.assertRaises(codec.DbcCodecError):
            codec.encode_text("温度 ☃\n", "gbk")

    def test_atomic_write_keeps_a_backup(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "a.dbc"
            codec.write_file(path, "one\n", "utf-8")
            codec.write_file(path, "two\n", "utf-8")
            self.assertEqual(path.read_text(), "two\n")
            self.assertEqual(Path(str(path) + ".bak").read_text(), "one\n")
            self.assertEqual([p.name for p in Path(folder).iterdir() if p.name.endswith(".tmp")], [])


if __name__ == "__main__":
    unittest.main()

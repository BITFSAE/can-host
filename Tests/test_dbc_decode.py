"""Decoding, encoding and bit placement of DBC signals."""

from __future__ import annotations

from pathlib import Path
import random
import struct
import time
import unittest

from cantools.database.can import Message, Signal

from canhost.dbc import codec
from canhost.dbc.catalog import build_catalog
from canhost.dbc.decode import DbcEncodeError, DbcLoadError, DecodeDb
from canhost.dbc.document import DbcDocument
from canhost.dbc.layout import INTEL, MOTOROLA, signal_bits, span_bytes

FIXTURE = Path(__file__).parent / "fixtures" / "dbc" / "sample.dbc"
CENTRAL = Path(__file__).resolve().parents[2] / "vehicle-interfaces" / "can"


def database() -> DecodeDb:
    return DecodeDb(FIXTURE.read_text(encoding="utf-8"))


class LayoutTests(unittest.TestCase):
    def test_bit_addresses_follow_the_vector_convention(self) -> None:
        self.assertEqual(signal_bits(0, 4, INTEL), [3, 2, 1, 0])
        self.assertEqual(signal_bits(12, 8, INTEL), [19, 18, 17, 16, 15, 14, 13, 12])
        # DBC @0: the start bit is the most significant bit; after bit 0 of a byte
        # the signal continues at bit 7 of the next byte.
        self.assertEqual(signal_bits(7, 16, MOTOROLA), list(range(7, -1, -1)) + list(range(15, 7, -1)))
        self.assertEqual(signal_bits(12, 8, MOTOROLA), [12, 11, 10, 9, 8, 23, 22, 21])
        self.assertEqual(span_bytes([3, 15]), 2)
        self.assertEqual(span_bytes([]), 0)

    def test_placement_agrees_with_the_cantools_encoder(self) -> None:
        """Set each raw bit in turn through cantools and see where it lands."""
        generator = random.Random(2024)
        checked = 0
        for _ in range(600):
            order = generator.choice([INTEL, MOTOROLA])
            length = generator.randint(1, 20)
            start = generator.randint(0, 63)
            bits = signal_bits(start, length, order)
            if max(bits) > 63 or min(bits) < 0:
                continue
            signal = Signal("s", start, length, "little_endian" if order == INTEL else "big_endian")
            message = Message(0x1, "m", 8, [signal], strict=False)
            for index in range(length):
                encoded = message.encode({"s": 1 << index}, scaling=False, strict=False)
                value = int.from_bytes(encoded, "little")
                self.assertEqual(value, 1 << bits[length - 1 - index], (order, start, length, index))
            checked += 1
        self.assertGreater(checked, 200)


class DecodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = database()

    def decode(self, name: str, data: bytes) -> dict:
        return self.db.decode(self.db.by_name[name], data)

    def test_intel_scaled_signed_and_value_table(self) -> None:
        result = self.decode("EngineData", bytes([0x88, 0x13, 0x50, 0x03, 0x64, 0, 0, 0]))
        values = result["values"]
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(values["Speed"]["value"], 50.0)           # 5000 * 0.01
        self.assertEqual(values["Speed"]["raw"], 5000)
        self.assertEqual(values["Temp"]["value"], 40)                    # 0x50 - 40
        self.assertEqual(values["Gear"]["label"], "D")
        self.assertEqual(values["Gear"]["raw_text"], "3 (0x3)")
        self.assertAlmostEqual(values["Throttle"]["value"], 40.0)        # 100 * 0.4

    def test_signed_negative_value_and_range_flag(self) -> None:
        result = self.decode("EngineData", bytes([0, 0, 0xD8, 0, 0, 0, 0, 0]))
        self.assertEqual(result["values"]["Temp"]["raw"], -40)
        self.assertEqual(result["values"]["Temp"]["value"], -80)
        self.assertTrue(result["values"]["Temp"]["in_range"])
        outside = self.decode("EngineData", bytes([0, 0, 0, 0, 0xFF, 0, 0, 0]))
        self.assertFalse(outside["values"]["Throttle"]["in_range"])       # 255 * 0.4 = 102 > 100

    def test_motorola_signal(self) -> None:
        result = self.decode("MotorolaMsg", bytes([0xFF, 0x38, 0x5A, 0, 0, 0, 0, 0]))
        self.assertEqual(result["values"]["Torque"]["raw"], -200)
        self.assertAlmostEqual(result["values"]["Torque"]["value"], -20.0)
        self.assertEqual(result["values"]["Flags"]["raw"], 0x5A)

    def test_messages_whose_signals_are_listed_high_to_low_still_decode(self) -> None:
        """Regression: with ``sort_signals=None`` cantools cannot unpack these."""
        data = struct.pack("<hhhh", 100, 0, 0, -50)                      # Lo, -, -, Hi
        values = self.decode("Reversed", data)["values"]
        self.assertAlmostEqual(values["Lo"]["value"], 10.0)
        self.assertAlmostEqual(values["Hi"]["value"], -5.0)

    def test_multiplexed_signals_follow_the_selector(self) -> None:
        first = self.decode("MuxMsg", bytes([0, 0x34, 0x12, 9, 0, 0, 0, 0]))["values"]
        self.assertEqual(set(first), {"Sel", "Ana", "Common"})
        self.assertEqual(first["Ana"]["raw"], 0x1234)
        second = self.decode("MuxMsg", bytes([1, 0x0A, 0, 9, 0, 0, 0, 0]))["values"]
        self.assertEqual(set(second), {"Sel", "Cur", "Common"})
        self.assertAlmostEqual(second["Cur"]["value"], 1.0)

    def test_float_signal(self) -> None:
        data = struct.pack("<f", 1.5) + bytes(4)
        value = self.decode("FloatMsg", data)["values"]["Ratio"]
        self.assertEqual(value["value"], 1.5)

    def test_dlc_mismatch_is_reported_not_zero_filled(self) -> None:
        short = self.decode("EngineData", bytes([0x88, 0x13, 0x50]))
        self.assertEqual(short["status"], "dlc_short")
        self.assertEqual(set(short["values"]), {"Speed", "Temp"})        # only what fits completely
        longer = self.decode("EngineData", bytes(10))
        self.assertEqual(longer["status"], "dlc_long")
        self.assertIn("Speed", longer["values"])

    def test_standard_and_extended_ids_are_separate_keys(self) -> None:
        text = ('VERSION ""\n\nBS_:\n\nBU_: A\n\nBO_ 291 Std: 8 A\n SG_ S : 0|8@1+ (1,0) [0|0] "" A\n\n'
                'BO_ 2147483939 Ext: 8 A\n SG_ E : 0|8@1+ (1,0) [0|0] "" A\n')
        db = DecodeDb(text)
        self.assertEqual(db.message(291, False).name, "Std")
        self.assertEqual(db.message(291, True).name, "Ext")

    def test_long_signal_names_are_decoded_under_their_full_name(self) -> None:
        message = self.db.message(0x1806E5F4, True)
        self.assertIn("VeryLongSignalNameForTestingTheShortSymbol", [s.name for s in message.signals])

    def test_wide_raw_values_do_not_lose_precision_in_json(self) -> None:
        text = ('VERSION ""\n\nBS_:\n\nBU_: A\n\nBO_ 1 Big: 8 A\n'
                ' SG_ B : 0|64@1+ (1,0) [0|0] "" A\n')
        db = DecodeDb(text)
        entry = db.decode(db.by_name["Big"], bytes([0xFF] * 8))["values"]["B"]
        self.assertEqual(entry["raw"], str(2 ** 64 - 1))

    def test_unparseable_text_raises_a_load_error(self) -> None:
        with self.assertRaises(DbcLoadError):
            DecodeDb("this is not a dbc")

    def test_overlapping_signals_load_but_are_reported(self) -> None:
        text = ('VERSION ""\n\nBS_:\n\nBU_: A\n\nBO_ 1 Overlap: 8 A\n SG_ X : 0|8@1+ (1,0) [0|0] "" A\n'
                ' SG_ Y : 4|8@1+ (1,0) [0|0] "" A\n')
        db = DecodeDb(text)
        self.assertIsNotNone(db.strict_error)
        self.assertIn("Overlap", db.by_name)

    def test_decode_is_fast_enough_for_per_frame_recording(self) -> None:
        message = self.db.by_name["EngineData"]
        data = bytes([0x88, 0x13, 0x50, 0x03, 0x64, 0, 0, 0])
        started = time.perf_counter()
        for _ in range(5000):
            self.db.decode(message, data)
        self.assertLess(time.perf_counter() - started, 1.0)               # > 5k frames/s with huge margin


class EncodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = database()

    def test_decode_inverts_encode(self) -> None:
        message = self.db.by_name["EngineData"]
        data = self.db.encode(message, {"Speed": 50.0, "Temp": 40, "Gear": 3, "Throttle": 40.0})
        self.assertEqual(data, bytes([0x88, 0x13, 0x50, 0x03, 0x64, 0, 0, 0]))
        values = self.db.decode(message, data)["values"]
        self.assertAlmostEqual(values["Speed"]["value"], 50.0)

    def test_value_table_labels_can_be_encoded(self) -> None:
        message = self.db.by_name["EngineData"]
        data = self.db.encode(message, {"Gear": "R"})
        self.assertEqual(self.db.decode(message, data)["values"]["Gear"]["raw"], 1)
        with self.assertRaises(DbcEncodeError):
            self.db.encode(message, {"Gear": "Warp"})

    def test_values_outside_the_raw_range_are_rejected(self) -> None:
        with self.assertRaises(DbcEncodeError):
            self.db.encode(self.db.by_name["EngineData"], {"Speed": 99999})

    def test_out_of_declared_range_is_allowed_for_bench_testing(self) -> None:
        data = self.db.encode(self.db.by_name["EngineData"], {"Throttle": 101.6})   # raw 254, > 100 %
        self.assertEqual(data[4], 254)

    def test_multiplexed_messages_encode_the_selected_branch(self) -> None:
        message = self.db.by_name["MuxMsg"]
        data = self.db.encode(message, {"Sel": 1, "Cur": 2.5, "Ana": 999, "Common": 7})
        values = self.db.decode(message, data)["values"]
        self.assertEqual(set(values), {"Sel", "Cur", "Common"})
        self.assertAlmostEqual(values["Cur"]["value"], 2.5)


@unittest.skipUnless(CENTRAL.is_dir(), "兄弟仓库 vehicle-interfaces 不存在")
class TeamDatabaseTests(unittest.TestCase):
    def test_every_message_of_every_team_dbc_decodes_random_payloads(self) -> None:
        generator = random.Random(11)
        for path in sorted(CENTRAL.glob("*.dbc")):
            with self.subTest(path.name):
                text, _ = codec.read_file(path)
                db = DecodeDb(text)
                self.assertIsNone(db.strict_error)
                catalog = build_catalog(DbcDocument.parse(text))
                for entry in catalog["messages"]:
                    if entry["virtual"]:
                        continue
                    message = db.message(entry["id"], entry["extended"])
                    data = bytes(generator.randrange(256) for _ in range(message.length))
                    result = db.decode(message, data)
                    self.assertEqual(result["status"], "ok", entry["name"])
                    self.assertTrue(result["values"], entry["name"])


if __name__ == "__main__":
    unittest.main()

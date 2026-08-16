"""Tests for the shared IVT protocol and host-side configuration boundary."""

from __future__ import annotations

import unittest
from collections import deque

from TOOLS.bms_host.can_service import CanService
from TOOLS.bms_host.ivt import (
    BMS_CANB_CMD_ID,
    BMS_CANB_RSP_ID,
    BMS_CANB_RESULT_IDS,
    CHANNELS,
    IvtClient,
    IvtFrame,
    IVT_RESPONSE_MUXES,
    compare_readback,
    expected_bms_canb_config,
    factory_config,
    parse_config_response,
)


class FakeIvtTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[int, bytes]] = []
        self.received: deque[IvtFrame] = deque()

    def send(self, arbitration_id: int, data: list[int]) -> None:
        payload = bytes(data)
        self.sent.append((arbitration_id, payload))
        mux = payload[0]
        if mux == 0x7B:
            self.received.append(IvtFrame(0x511, bytes([0xBB, 0x12, 0x34, 0x56, 0x78, 0, 0, 0])))
        elif mux == 0x34:
            self.received.append(IvtFrame(0x511, bytes([0xB4, payload[1], payload[2], 0, 0, 0, 0, 0])))
        elif mux == 0x1F:
            self.received.append(IvtFrame(0x51A, bytes([0x9F, 0, 0, 0, 0, 0, 0, 0])))

    def receive(self, timeout: float) -> IvtFrame:
        if not self.received:
            raise TimeoutError("fake transport queue empty")
        return self.received.popleft()


class FullFakeIvtTransport:
    def __init__(self) -> None:
        self.received: deque[IvtFrame] = deque()
        self.serial = 0x12345678

    def send(self, arbitration_id: int, data: list[int]) -> None:
        mux = data[0]
        response_id = BMS_CANB_RSP_ID
        if mux == 0x7B:
            payload = [0xBB, *self.serial.to_bytes(4, "big"), 0, 0, 0]
        elif mux == 0x79:
            payload = [0xB9, 0x02, 0x1F, 0x30, 0, 1, 1, 0]
        elif mux == 0x7A:
            payload = [0xBA, 1, 2, 3, 4, 5, 6, 7]
        elif mux == 0x7C:
            payload = [0xBC, 0x10, 0x20, 0x30, 0x40, 0, 0, 0]
        elif mux == 0x74:
            payload = [0xB4, 1, 1, 0, 0, 0, 0, 0]
        elif 0x60 <= mux <= 0x67:
            channel = CHANNELS[mux - 0x60]
            payload = [channel.rsp_mux, 0x42,
                       (channel.default_period_ms >> 8) & 0xFF, channel.default_period_ms & 0xFF, 0, 0, 0, 0]
        elif 0x50 <= mux <= 0x57:
            channel = CHANNELS[mux - 0x50]
            can_id = BMS_CANB_RESULT_IDS[channel.index]
            payload = [channel.can_id_rsp_mux, (can_id >> 8) & 0xFF, can_id & 0xFF,
                       *self.serial.to_bytes(4, "big"), 0]
        elif mux == 0x5D:
            payload = [0x9D, (BMS_CANB_CMD_ID >> 8) & 0xFF, BMS_CANB_CMD_ID & 0xFF,
                       *self.serial.to_bytes(4, "big"), 0]
        elif mux == 0x5F:
            payload = [0x9F, (BMS_CANB_RSP_ID >> 8) & 0xFF, BMS_CANB_RSP_ID & 0xFF,
                       *self.serial.to_bytes(4, "big"), 0]
        elif mux in (0x75, 0x76):
            payload = [0xB5 if mux == 0x75 else 0xB6, 0, 0, 0, 0, 0, 0, 0]
        else:
            raise AssertionError(f"unexpected IVT request mux 0x{mux:02X}")
        self.received.append(IvtFrame(response_id, bytes(payload)))

    def receive(self, timeout: float) -> IvtFrame:
        if not self.received:
            raise TimeoutError("fake transport queue empty")
        return self.received.popleft()


def readback_for(expected: dict) -> dict:
    channels = []
    for channel in CHANNELS:
        item = dict(expected["channels"][channel.name])
        item.update({"name": channel.name, "unit": channel.unit, "index": channel.index})
        channels.append(item)
    return {
        "bitrate": expected["bitrate"],
        "mode": {
            "current_name": expected["mode"]["current"],
            "startup_name": expected["mode"]["startup"],
        },
        "channels": channels,
        "can_ids": dict(expected["can_ids"]),
        "thresholds": {
            "positive": dict(expected["thresholds"]["positive"]),
            "negative": dict(expected["thresholds"]["negative"]),
        },
    }


class IvtProtocolTest(unittest.TestCase):
    def test_client_uses_standard_request_and_response_ids(self) -> None:
        transport = FakeIvtTransport()
        client = IvtClient(transport.send, transport.receive)
        self.assertEqual(client.read_serial_number(), 0x12345678)
        self.assertEqual(transport.sent[0], (0x411, bytes([0x7B, 0, 0, 0, 0, 0, 0, 0])))

    def test_response_id_change_accepts_new_response_id(self) -> None:
        transport = FakeIvtTransport()
        client = IvtClient(transport.send, transport.receive)
        client.set_response_can_id(0x51A, 0x12345678)
        self.assertEqual(client.rsp_id, 0x51A)
        self.assertEqual(transport.sent[0][0], 0x411)

    def test_config_response_decodes_all_db1_fields(self) -> None:
        frame = IvtFrame(0x511, bytes([0xA0, 0xE2, 0x00, 0x3C, 0, 0, 0, 0]))
        parsed = parse_config_response(frame)
        self.assertEqual(parsed["mode_name"], "cyclic")
        self.assertEqual(parsed["byte_order"], "little")
        self.assertTrue(parsed["report_errors"])
        self.assertTrue(parsed["invert_sign"])
        self.assertEqual(parsed["period_ms"], 60)

    def test_readback_classifies_target_and_factory_defaults(self) -> None:
        target = expected_bms_canb_config()
        matching = readback_for(target)
        self.assertEqual(compare_readback(matching, target)["status_name"], "已配置且一致")

        factory = factory_config()
        factory_readback = {
            "bitrate": 500000,
            "mode": {"current_name": "run", "startup_name": "run"},
            "channels": [
                dict(factory["channels"][channel.name], name=channel.name)
                for channel in CHANNELS
            ],
            "can_ids": dict(factory["can_ids"]),
            "thresholds": {
                "positive": dict(factory["thresholds"]["positive"]),
                "negative": dict(factory["thresholds"]["negative"]),
            },
        }
        self.assertEqual(compare_readback(factory_readback, target)["status_name"], "未配置")

        mismatch = readback_for(target)
        mismatch["channels"][1]["period_ms"] = 61
        result = compare_readback(mismatch, target)
        self.assertEqual(result["status_name"], "配置不符")
        self.assertTrue(any(item["field"] == "channel.U1.period_ms" for item in result["differences"]))

    def test_complete_readback_uses_shared_target_definition(self) -> None:
        transport = FullFakeIvtTransport()
        client = IvtClient(transport.send, transport.receive, cmd_id=BMS_CANB_CMD_ID, rsp_id=BMS_CANB_RSP_ID)
        readback = client.readback(bitrate=500000)
        comparison = compare_readback(readback, expected_bms_canb_config())
        self.assertEqual(comparison["status_name"], "已配置且一致")
        self.assertEqual(readback["can_ids"]["response"], BMS_CANB_RSP_ID)
        self.assertEqual(readback["thresholds"]["negative"]["threshold_a"], 0)

    def test_bms_result_ids_are_the_expected_eight_ids(self) -> None:
        self.assertEqual(BMS_CANB_RESULT_IDS, tuple(range(0x512, 0x51A)))
        self.assertIn(0x9D, IVT_RESPONSE_MUXES)
        self.assertIn(0x9F, IVT_RESPONSE_MUXES)


class IvtServiceBoundaryTest(unittest.TestCase):
    def test_ivt_write_is_rejected_on_can1(self) -> None:
        service = CanService()
        try:
            service.connection.update({"connected": True, "mode": "pcan", "bus_profile": "can1", "bitrate": 500000})
            result = service.read_ivt_config()
            self.assertFalse(result["ok"])
            self.assertIn("CANB", result["error"])
        finally:
            service.disconnect()

    def test_ivt_write_is_rejected_for_simulation(self) -> None:
        service = CanService()
        try:
            service.connection.update({"connected": True, "mode": "simulation", "bus_profile": "canb", "bitrate": 500000})
            result = service.read_ivt_config()
            self.assertFalse(result["ok"])
            self.assertIn("真实 PCAN", result["error"])
        finally:
            service.disconnect()

    def test_bench_command_reuses_pcan_bench_model(self) -> None:
        from TOOLS import pcan_bms_bench

        service = CanService()
        try:
            service.connection.update({"mode": "bench", "bus_profile": "can1"})
            service.bench_module = pcan_bms_bench
            service.bench_model = pcan_bms_bench.BenchModel()
            service.bench_command_processor = pcan_bms_bench.CommandProcessor(
                service.bench_model, pcan_bms_bench.BmsMonitorState()
            )
            result = service.bench_command("cell 1 4000")
            self.assertTrue(result["ok"])
            self.assertEqual(service.bench_model.get_cell_mv(1), 4000)
        finally:
            service.disconnect()


if __name__ == "__main__":
    unittest.main()

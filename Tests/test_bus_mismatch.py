"""总线疑似接反判定：只按对侧独有帧提示，不改变任何连接或命令门控。

接反证据来自真实 PCAN 连接的接收帧：CAN1 档案收到整车 CANB 节点帧、
CANB 档案收到 F405 或从控的 CAN1 专属扩展帧。判定结果只写进快照供界面提醒。
"""

import time
import unittest

from canhost.bms.protocol import is_can1_bus_signature, is_can1_slave_frame
from canhost.decoders import CANB_ONLY_NODE_STD_IDS, CanFrame
from canhost.transport import CanService


def pcan_service(profile="can1", protocol_kind="bms"):
    service = CanService(protocol_kind=protocol_kind)
    service.connection.update({"connected": True, "mode": "pcan", "channel": "PCAN_USBBUS1",
                               "bitrate": 500000, "bus_profile": profile, "status": "已连接",
                               "error": None})
    return service


def feed(service, frame):
    service._ingest(frame)


def rx(can_id, data=b"\x00" * 8, extended=False):
    return CanFrame(can_id, data, extended, time.time(), "rx")


def tx(can_id, data=b"\x00" * 8, extended=False):
    return CanFrame(can_id, data, extended, time.time(), "tx")


class Can1SlaveFrameTest(unittest.TestCase):
    def test_slave_voltage_and_temperature_ranges(self):
        self.assertTrue(is_can1_slave_frame(0x180050F3, True))
        self.assertTrue(is_can1_slave_frame(0x180050F3 + (35 << 16), True))
        self.assertTrue(is_can1_slave_frame(0x184050F3, True))
        self.assertTrue(is_can1_slave_frame(0x184050F3 + (5 << 16), True))

    def test_other_frames_are_not_slave_frames(self):
        self.assertFalse(is_can1_slave_frame(0x186050F4, True))   # F405 电池总状态
        self.assertFalse(is_can1_slave_frame(0x5A2, False))       # 风扇实际状态
        self.assertFalse(is_can1_slave_frame(0x180050F3 + (36 << 16), True))
        self.assertFalse(is_can1_slave_frame(0x180050F3 + 1, True))

    def test_f405_periodic_frames_are_can1_signatures(self):
        self.assertTrue(is_can1_bus_signature(0x186050F4, True))
        self.assertTrue(is_can1_bus_signature(0x187650F4, True))
        self.assertFalse(is_can1_bus_signature(0x4B0, False))


class CanbOnlyIdSetTest(unittest.TestCase):
    def test_vehicle_nodes_are_canb_only(self):
        for can_id in (0x4A0, 0x4B0, 0x4B1, 0x4B2, 0x502, 0x521, 0x5A0, 0x5AE, 0x201, 0x291):
            self.assertIn(can_id, CANB_ONLY_NODE_STD_IDS)

    def test_ivt_result_ids_are_not_canb_only(self):
        # IVT 同时登记在整车 DBC 中，而实体接在 CAN1，不能作为接反证据。
        for can_id in range(0x512, 0x51A):
            self.assertNotIn(can_id, CANB_ONLY_NODE_STD_IDS)


class BusMismatchTest(unittest.TestCase):
    def test_can1_profile_hearing_vehicle_nodes_reports_canb(self):
        service = pcan_service("can1")
        for can_id in (0x5A2, 0x5A0, 0x4B0):
            feed(service, rx(can_id))
        snapshot = service.snapshot()
        mismatch = snapshot["connection"]["bus_mismatch"]
        self.assertEqual(mismatch["expected"], "can1")
        self.assertEqual(mismatch["detected"], "canb")
        self.assertGreaterEqual(mismatch["evidence_count"], 2)

    def test_canb_profile_hearing_slave_frames_reports_can1(self):
        service = pcan_service("canb")
        feed(service, rx(0x180050F3, extended=True))
        feed(service, rx(0x184050F3, extended=True))
        snapshot = service.snapshot()
        mismatch = snapshot["connection"]["bus_mismatch"]
        self.assertEqual(mismatch["expected"], "canb")
        self.assertEqual(mismatch["detected"], "can1")

    def test_canb_profile_hearing_f405_periodic_frames_reports_can1(self):
        service = pcan_service("canb")
        feed(service, rx(0x186050F4, b"\x00" * 7, extended=True))
        feed(service, rx(0x187650F4, extended=True))
        mismatch = service.snapshot()["connection"]["bus_mismatch"]
        self.assertEqual((mismatch["expected"], mismatch["detected"]), ("canb", "can1"))

    def test_vehicle_service_reports_mismatch(self):
        service = pcan_service("canb", protocol_kind="vehicle")
        for _ in range(2):
            feed(service, rx(0x180050F3, extended=True))
        mismatch = service.vehicle_snapshot()["connection"]["bus_mismatch"]
        self.assertEqual((mismatch["expected"], mismatch["detected"]), ("canb", "can1"))

    def test_matching_traffic_reports_nothing(self):
        service = pcan_service("can1")
        for _ in range(5):
            feed(service, rx(0x186050F4, extended=True))
            feed(service, rx(0x180050F3, extended=True))
        self.assertNotIn("bus_mismatch", service.snapshot()["connection"])

    def test_tx_frames_do_not_count(self):
        service = pcan_service("can1")
        for _ in range(5):
            feed(service, tx(0x5A2))
        self.assertNotIn("bus_mismatch", service.snapshot()["connection"])

    def test_single_frame_is_not_enough(self):
        service = pcan_service("can1")
        feed(service, rx(0x5A2))
        self.assertNotIn("bus_mismatch", service.snapshot()["connection"])

    def test_evidence_outside_two_second_window_restarts_count(self):
        service = pcan_service("can1")
        feed(service, rx(0x5A2))
        with service.lock:
            service._bus_evidence["canb_last"] = time.monotonic() - 2.1
        feed(service, rx(0x5A0))
        self.assertEqual(service._bus_evidence["canb_count"], 1)
        self.assertNotIn("bus_mismatch", service.snapshot()["connection"])

    def test_stale_evidence_disappears(self):
        service = pcan_service("can1")
        feed(service, rx(0x5A2))
        feed(service, rx(0x5A0))
        self.assertIn("bus_mismatch", service.snapshot()["connection"])
        with service.lock:
            service._bus_evidence["canb_last"] = time.monotonic() - 10.0
        self.assertNotIn("bus_mismatch", service.snapshot()["connection"])

    def test_simulation_and_replay_are_ignored(self):
        service = pcan_service("can1")
        service.connection["mode"] = "simulation"
        for _ in range(3):
            feed(service, rx(0x5A2))
        self.assertNotIn("bus_mismatch", service.snapshot()["connection"])
        service.connection["mode"] = "replay"
        feed(service, rx(0x5A2))
        self.assertNotIn("bus_mismatch", service.snapshot()["connection"])

    def test_disconnected_connection_has_no_mismatch(self):
        service = pcan_service("can1")
        for _ in range(3):
            feed(service, rx(0x5A2))
        service.connection["connected"] = False
        self.assertNotIn("bus_mismatch", service.snapshot()["connection"])

    def test_quick_snapshot_carries_mismatch(self):
        # get_quick_snapshot() 只使用整车服务；快照条也用于风扇页的接反提示。
        service = pcan_service("canb", protocol_kind="vehicle")
        for _ in range(2):
            feed(service, rx(0x180050F3, extended=True))
        self.assertEqual(service.quick_snapshot()["connection"]["bus_mismatch"]["detected"], "can1")

    def test_connect_resets_evidence(self):
        service = pcan_service("can1")
        for can_id in (0x5A2, 0x5A0, 0x4B0):
            feed(service, rx(can_id))
        self.assertIn("bus_mismatch", service.snapshot()["connection"])
        result = service.connect({"mode": "simulation", "bus_profile": "can1",
                                  "channel": None, "bitrate": 500000})
        try:
            self.assertTrue(result.get("ok"))
            self.assertNotIn("bus_mismatch", service.snapshot()["connection"])
            with service.lock:
                self.assertEqual(service._bus_evidence["canb_count"], 0)
                self.assertEqual(service._bus_evidence["can1_count"], 0)
        finally:
            service.disconnect()


if __name__ == "__main__":
    unittest.main()

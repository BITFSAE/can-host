"""PCAN-Basic attached-channel discovery tests."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from can.interfaces.pcan.basic import (
    FEATURE_FD_CAPABLE,
    FEATURE_IO_CAPABLE,
    PCAN_CHANNEL_AVAILABLE,
    PCAN_CHANNEL_OCCUPIED,
    PCAN_ERROR_OK,
    PCAN_USBBUS1,
    PCAN_USBBUS2,
)

from canhost.pcan_channels import MANUAL_PCAN_CHANNELS, discover_pcan_channels
from canhost.app import Api
from canhost.transport import CanService


class FakePcanBasic:
    def __init__(self, status=PCAN_ERROR_OK, attached=()) -> None:
        self.status = status
        self.attached = attached

    def GetValue(self, channel, parameter):
        return self.status, self.attached

    def GetErrorText(self, status):
        return PCAN_ERROR_OK, b"Parameter is not supported"


def channel_info(handle, controller, condition, name=b"PCAN-USB Pro FD"):
    return SimpleNamespace(
        channel_handle=handle,
        device_type=5,
        controller_number=controller,
        device_features=int(FEATURE_FD_CAPABLE) | int(FEATURE_IO_CAPABLE),
        device_name=name,
        device_id=0,
        channel_condition=condition,
    )


class PcanChannelDiscoveryTest(unittest.TestCase):
    def test_lists_only_attached_channels_with_hardware_identity(self) -> None:
        result = discover_pcan_channels(FakePcanBasic(attached=[
            channel_info(PCAN_USBBUS2, 1, PCAN_CHANNEL_OCCUPIED),
            channel_info(PCAN_USBBUS1, 0, PCAN_CHANNEL_AVAILABLE),
        ]))

        self.assertTrue(result["ok"])
        self.assertTrue(result["automatic"])
        self.assertEqual(result["channels"], ["PCAN_USBBUS1", "PCAN_USBBUS2"])
        self.assertIn("PCAN-USB Pro FD · CAN 1 · 可用", result["channel_details"][0]["label"])
        self.assertEqual(result["channel_details"][1]["condition"], "occupied")
        self.assertEqual(result["channel_details"][0]["features"], ["FD", "IO"])

    def test_successful_empty_scan_does_not_invent_channels(self) -> None:
        result = discover_pcan_channels(FakePcanBasic())
        self.assertTrue(result["ok"])
        self.assertEqual(result["channels"], [])
        self.assertIn("未检测到", result["message"])

    def test_unsupported_enumeration_keeps_manual_compatibility_list(self) -> None:
        result = discover_pcan_channels(FakePcanBasic(status=0x08000000))
        self.assertFalse(result["ok"])
        self.assertFalse(result["automatic"])
        self.assertEqual(result["channels"], MANUAL_PCAN_CHANNELS)
        self.assertIn("手动通道列表", result["message"])
        self.assertEqual(result["error"], "Parameter is not supported")

    def test_bootstrap_and_refresh_expose_the_same_discovery_shape(self) -> None:
        scan = {
            "ok": True, "automatic": True,
            "channels": ["PCAN_USBBUS1"],
            "channel_details": [{"channel": "PCAN_USBBUS1", "label": "USB 1"}],
            "message": "检测到 1 个 PCAN 通道", "error": None,
        }
        with patch("canhost.app.discover_pcan_channels", return_value=scan):
            api = Api()
            try:
                bootstrap = api.bootstrap()
                self.assertEqual(bootstrap["channels"], scan["channels"])
                self.assertEqual(bootstrap["channel_details"], scan["channel_details"])
                self.assertEqual(api.refresh_pcan_channels(), scan)
            finally:
                api.close()

    def test_real_connection_requires_an_explicit_channel(self) -> None:
        service = CanService(allow_simulation=False)
        result = service.connect({"mode": "pcan", "bus_profile": "can1"})
        self.assertFalse(result["ok"])
        self.assertIn("未选择 PCAN 通道", result["error"])


if __name__ == "__main__":
    unittest.main()

"""Legacy 250k connection, receive, persistence and write boundaries."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from canhost.app import Api
from canhost.bms.protocol import BmsProtocol
from canhost.decoders import CanFrame, decode_legacy_charger_feedback, decode_bms_fan_detail, decode_bms_hv_status
from canhost.transport import CanService


class LegacyChargerTest(unittest.TestCase):
    def test_full_charge_status_and_compatibility(self):
        now = [1.0]
        protocol = BmsProtocol(clock=lambda: now[0])
        for state in range(7):
            data = bytes([0x17, 3, 232, 7, 208, 0x10 | state, 30, 0])
            protocol.ingest(CanFrame(0x186950F4, data, True))
            hv = protocol.snapshot({"bus_profile": "can1"})["hv"]
            self.assertEqual(hv["legacy_end_state"], state)
            self.assertEqual(hv["legacy_end_hold_s"], 30)
            self.assertEqual(hv["success_ms"], 1000)
            self.assertEqual(hv["failure_ms"], 2000)
            self.assertTrue(hv["hv_acc"] and hv["charge_button"] and hv["external_safety_event"])
        now[0] += 1.6
        self.assertGreater(protocol.snapshot({"bus_profile": "can1"})["hv"]["age"], 1.5)
        for data in [bytes(5), bytes(8), bytes([0,0,0,0,0,0x34,30,0])]:
            self.assertIsNone(decode_bms_hv_status(data)["legacy_end_state"])
        self.assertEqual(decode_bms_hv_status(bytes([0,0,0,0,0,0x1f,255,0]))["legacy_end_name"], "状态未知")
        with self.assertRaises(ValueError):
            decode_bms_hv_status(bytes(4))

    def test_shared_charge_end_v2(self):
        for state in range(8):
            hv = decode_bms_hv_status(bytes([0, 0, 0, 0, 0, 0x20 | state, 10, 0]))
            self.assertEqual(hv["legacy_end_state"], state)
            self.assertEqual(hv["legacy_end_hold_s"], 10)
            self.assertEqual(hv["charge_end_target_s"], 10)
        self.assertEqual(hv["legacy_end_name"], "单体电压停止")
        invalid = decode_bms_hv_status(bytes([0, 0, 0, 0, 0, 0x22, 11, 0]))
        self.assertIsNone(invalid["legacy_end_hold_s"])
        old_reserved = decode_bms_hv_status(bytes([0, 0, 0, 0, 0, 0x17, 0, 0]))
        self.assertEqual(old_reserved["legacy_end_name"], "状态未知")

    def test_decoder_and_freshness(self):
        now = [1.0]
        protocol = BmsProtocol(clock=lambda: now[0])
        frame = CanFrame(0x18FF50E5, bytes.fromhex('16 44 00 1E 10'), True)
        protocol.ingest(frame)
        connection = {'bus_profile': 'canb', 'bitrate': 250000}
        status = protocol.snapshot(connection)['runtime_diag']
        self.assertEqual(status['charger_feedback_voltage_v'], 570.0)
        self.assertEqual(status['charger_feedback_current_a'], 3.0)
        self.assertEqual(status['charger_feedback_state'], 0x10)
        self.assertTrue(status['charger_feedback_fresh'])
        now[0] += 0.6
        protocol.ingest(CanFrame(frame.arbitration_id, frame.data[:4], True))
        protocol.ingest(CanFrame(frame.arbitration_id, frame.data, False))
        protocol.ingest(CanFrame(frame.arbitration_id, frame.data, True, direction='tx'))
        self.assertFalse(protocol.snapshot(connection)['runtime_diag']['charger_feedback_fresh'])
        self.assertNotIn('charger_feedback_voltage_v', protocol.snapshot({'bus_profile': 'can1'})['runtime_diag'])
        with self.assertRaises(ValueError):
            decode_legacy_charger_feedback(b'\x00' * 4)
        self.assertEqual(decode_bms_fan_detail(bytes.fromhex('04 B0 01 2C 01 2C 04 21'))['power_source_name'], '充电车 35W')

    def test_real_transport_connects_250k_and_guards_writes(self):
        service = CanService('vehicle')
        bus = MagicMock()
        def recv(timeout):
            time.sleep(0.005)
            return None
        bus.recv.side_effect = recv
        try:
            with patch('can.Bus', return_value=bus) as factory:
                result = service.connect({'mode': 'pcan', 'bus_profile': 'canb',
                                          'channel': 'PCAN_USBBUS2', 'bitrate': 250000})
                self.assertTrue(result['ok'], result)
                self.assertEqual(factory.call_args.kwargs['bitrate'], 250000)
                service._ingest(CanFrame(0x18FF50E5, bytes.fromhex('16 44 00 1E 00'), True))
                self.assertTrue(service.canb_bms_snapshot()['runtime_diag']['charger_feedback_fresh'])
                self.assertIn('Legacy', service._monitor_frame_name(0x18FF50E5, True))
                self.assertFalse(service.send_command('charge_config', {}, True)['ok'])
                self.assertFalse(service.send_fan_command('query', {}, True)['ok'])
                self.assertFalse(service.send_battery_fan_command('query', {}, True)['ok'])
                spec = {'id': '0x1806E5F4', 'extended': True, 'data': '16 44 00 1E 00', 'cycle_ms': 1000}
                self.assertFalse(service.send_monitor_frame(spec, True)['ok'])
                self.assertFalse(service.configure_monitor_periodic('charger', spec, True, True)['ok'])
                bus.send.assert_not_called()
        finally:
            service.disconnect()

    def test_api_250k_and_preferences(self):
        api = Api()
        try:
            with patch.object(api._vehicle_service, 'connect', return_value={'ok': True}) as connect:
                result = api.connect_vehicle({'bus_profile': 'canb', 'bitrate': 250000,
                                              'channel': 'PCAN_USBBUS2', 'auto_record': False})
                self.assertTrue(result['ok'])
                self.assertEqual(connect.call_args.args[0]['bitrate'], 250000)
            self.assertFalse(api.connect_vehicle({'mode': 'simulation', 'bitrate': 250000})['ok'])
            self.assertFalse(api.connect_ivt({'bitrate': 250000})['ok'])
            with patch.object(api, '_save_workbench_preference', return_value={'ok': True}) as save:
                self.assertTrue(api.set_connection_preferences({'can1Channel': 'A', 'canbChannel': 'B', 'canbBitrate': 250000})['ok'])
                self.assertEqual(save.call_args.args[1]['canbBitrate'], 250000)
                self.assertFalse(api.set_connection_preferences({'can1Channel': 'A', 'canbChannel': 'B', 'canbBitrate': 125000})['ok'])
        finally:
            api.close()

    def test_record_and_replay_250k(self):
        service = CanService('vehicle')
        replay = CanService()
        try:
            service.connection.update({'connected': True, 'mode': 'pcan', 'bus_profile': 'canb', 'bitrate': 250000})
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'legacy.bmslog'
                self.assertTrue(service.start_recording(str(path))['ok'])
                service._ingest(CanFrame(0x18FF50E5, bytes.fromhex('16 44 00 1E 00'), True, time.time()))
                service.stop_recording()
                result = replay.load_replay(str(path))
                self.assertTrue(result['ok'], result)
                self.assertEqual(replay.connection['bitrate'], 250000)
                self.assertEqual(replay.connection['bus_profile'], 'canb')
                replay.disconnect()
        finally:
            service.disconnect()
            replay.disconnect()

    def test_single_channel_failed_handover_restores_250k(self):
        api = Api()
        try:
            api._vehicle_service.connection.update({
                'connected': True, 'mode': 'pcan', 'bus_profile': 'canb',
                'channel': 'PCAN_USBBUS2', 'bitrate': 250000,
            })
            with patch.object(api._service, 'connect', return_value={'ok': False, 'error': 'open failed'}), \
                    patch.object(api._vehicle_service, 'connect', return_value={'ok': True}) as restore:
                result = api.connect_can({'mode': 'pcan', 'bus_profile': 'can1',
                                          'channel': 'PCAN_USBBUS2', 'bitrate': 500000,
                                          'auto_record': False})
            self.assertFalse(result['ok'])
            self.assertIn('原 CANB 连接已恢复', result['error'])
            self.assertEqual(restore.call_args.args[0]['bitrate'], 250000)
            self.assertEqual(restore.call_args.args[0]['channel'], 'PCAN_USBBUS2')
        finally:
            api.close()

    def test_channel_swap_and_rollback_preserve_250k(self):
        for fail_swap in (False, True):
            with self.subTest(fail_swap=fail_swap):
                api = Api()
                try:
                    api._service.connection.update({
                        'connected': True, 'mode': 'pcan', 'bus_profile': 'can1',
                        'channel': 'PCAN_USBBUS1', 'bitrate': 500000,
                    })
                    api._vehicle_service.connection.update({
                        'connected': True, 'mode': 'pcan', 'bus_profile': 'canb',
                        'channel': 'PCAN_USBBUS2', 'bitrate': 250000,
                    })
                    attempts = []

                    def connect_vehicle(config):
                        attempts.append(config)
                        if fail_swap and len(attempts) == 1:
                            return {'ok': False, 'error': 'open failed'}
                        return {'ok': True}

                    with patch.object(api, '_physical_bus_mismatch', return_value=True), \
                            patch.object(api._service, 'connect', return_value={'ok': True}) as main_connect, \
                            patch.object(api._vehicle_service, 'connect', side_effect=connect_vehicle):
                        result = api.swap_mismatched_bus_channels({'auto_record': False})
                    self.assertEqual(result['ok'], not fail_swap)
                    self.assertEqual(attempts[0]['channel'], 'PCAN_USBBUS1')
                    self.assertTrue(all(config['bitrate'] == 250000 for config in attempts))
                    self.assertTrue(all(call.args[0]['bitrate'] == 500000 for call in main_connect.call_args_list))
                    self.assertEqual(len(attempts), 2 if fail_swap else 1)
                    if fail_swap:
                        self.assertEqual(attempts[1]['channel'], 'PCAN_USBBUS2')
                        self.assertIn('原 CAN1/CANB 连接已恢复', result['error'])
                finally:
                    api.close()

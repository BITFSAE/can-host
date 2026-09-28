"""V4 文档示例、参数边界与状态过期检查。"""
import unittest
from canhost.decoders import CanFrame, build_fan_command, decode_fan_profile, crc8_sae_j1850, fan_ack_matches
from canhost.vehicle.protocol import VehicleProtocol

class FanV4Test(unittest.TestCase):
    def test_document_profile_example(self):
        result = decode_fan_profile(bytes.fromhex('04 02 33 1E 28 0A 10 01'))
        self.assertEqual(result['profile'], 2)
        self.assertEqual(result['effective'], [3,3])
        self.assertEqual(result['cap_pct'], [30,40])
        self.assertTrue(result['lease_active'])
        self.assertFalse(result['derate_requested'])

    def test_document_command(self):
        frame=build_fan_command('fan_profile', {'_sequence':42,'profile':2,'lease_s':10})
        self.assertEqual(frame.data[:7], bytes.fromhex('09 2A 02 0A 00 00 00'))
        self.assertEqual(frame.data[7], crc8_sae_j1850(frame.data[:7]))
        self.assertTrue(fan_ack_matches('fan_profile', {'opcode':9}))
        self.assertFalse(fan_ack_matches('fan_profile', {'opcode':1}))

    def test_command_boundaries(self):
        for profile,lease in [(-1,10),(3,10),(1,0),(1,61)]:
            with self.assertRaises(ValueError):
                build_fan_command('fan_profile', {'profile':profile,'lease_s':lease})
        self.assertEqual(build_fan_command('fan_clear_faults').data[:7], bytes.fromhex('0A 00 A5 00 00 00 00'))

    def test_unknown_version(self):
        self.assertFalse(decode_fan_profile(bytes([5,2,0,0,0,0,0,0]))['supported'])
        with self.assertRaises(ValueError):decode_fan_profile(bytes(7))

    def test_mixed_strategy_faults(self):
        result=decode_fan_profile(bytes.fromhex('04 01 67 64 64 00 2F 83'))
        self.assertEqual(result['effective_names'], ['临界高温','停机'])
        self.assertEqual(result['locked'],[True,True])
        self.assertTrue(result['invalid_temperature'])
        self.assertEqual(result['limit_reason'],8)

    def test_protocol_age(self):
        now=[1.0]
        protocol=VehicleProtocol(clock=lambda:now[0])
        protocol.ingest(CanFrame(0x5AF,bytes.fromhex('04 02 33 1E 28 0A 10 01'),False))
        self.assertEqual(protocol.fan['profile_status']['profile'],2)
        now[0]=3.0
        self.assertEqual(protocol.snapshot({})['fan']['profile_status_age'],2.0)

class FanV4CommandGateTest(unittest.TestCase):
    def setUp(self):
        from canhost.transport import CanService
        from unittest.mock import MagicMock
        self.service = CanService(protocol_kind='vehicle')
        self.now = [10.0]
        self.service.protocol = VehicleProtocol(clock=lambda: self.now[0])
        self.service.connection.update(connected=True, mode='pcan', bus_profile='canb', bitrate=500000)
        self.service.bus = MagicMock()

    def tearDown(self):
        self.service.disconnect()

    def receive_profile(self, version=4):
        self.service.protocol.ingest(CanFrame(0x5AF, bytes([version,1,0x11,100,100,0,0,3]), False))

    def test_unknown_stale_and_future_versions_block_writes(self):
        for version in (None, 5, 4):
            if version is not None:
                self.receive_profile(version)
            if version == 4:
                self.now[0] += 1.6
            for name, values in [('fan_profile', {'profile':1}), ('fan_clear_faults', {}),
                                 ('fan_control', {'mode':1,'lease_s':10}), ('fan_restore_defaults', {})]:
                with self.subTest(version=version, name=name):
                    self.assertFalse(self.service.send_fan_command(name, values, True)['ok'])
        self.service.bus.send.assert_not_called()

    def test_legacy_writes_removed_and_no_calibration_api(self):
        from canhost.app import Api
        self.receive_profile()
        for name in ('fan_curve','fan_curve_ch2','fan_failsafe','fan_calib'):
            with self.assertRaises(ValueError):
                build_fan_command(name, {})
            self.assertFalse(self.service.send_fan_command(name, {}, True)['ok'])
        for name in ('start_fan_calibration','stop_fan_calibration','confirm_dcdc_ready',
                     'export_fan_calibration','choose_export_fan_calibration'):
            self.assertFalse(hasattr(Api, name))
        self.service.bus.send.assert_not_called()

    def test_fresh_profile_write_matches_ack_and_wire(self):
        self.receive_profile()
        def ack(message, timeout):
            data = message.data
            self.service.protocol.ingest(CanFrame(0x5A5, bytes([data[0],data[1],0,0,0,0,0,0]), False))
        self.service.bus.send.side_effect = ack
        result = self.service.send_fan_command('fan_profile', {'profile':2,'lease_s':10}, True)
        self.assertTrue(result['ok'], result)
        self.assertEqual(bytes(self.service.bus.send.call_args.args[0].data[:7]), bytes([9,1,2,10,0,0,0]))

    def test_query_can_discover_version_without_profile(self):
        def ack(message, timeout):
            self.service.protocol.ingest(CanFrame(0x5A5, bytes([5,message.data[1],0,0,0,0,0,0]), False))
        self.service.bus.send.side_effect = ack
        self.assertTrue(self.service.send_fan_command('fan_query', {}, True)['ok'])

    def test_battery_calibration_blocks_new_v4_actions(self):
        self.receive_profile()
        self.service.battery_fan_calib_session.status = 'running'
        try:
            for name in ('fan_profile','fan_clear_faults'):
                self.assertFalse(self.service.send_fan_command(name, {'profile':1}, True)['ok'])
            self.service.bus.send.assert_not_called()
        finally:
            self.service.battery_fan_calib_session.status = 'idle'

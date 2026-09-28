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

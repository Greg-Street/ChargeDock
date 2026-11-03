import unittest
from types import SimpleNamespace
from unittest.mock import patch

import device_proto as dp


class ProtocolTests(unittest.TestCase):
    def test_keyboard_charging_reply_is_online_at_85_percent(self):
        charging = bytes.fromhex("13 0a 01 00 04 05 55 10 00 00 00 00 00 00 00 00 00 00 00 8c")
        self.assertEqual(dp.parse_aula(charging), (85, True))

    def test_dedicated_keyboard_battery_reply_reports_charging_without_status_push(self):
        charging = bytes.fromhex("13 4a 01 00 02 60 10 00 00 00 00 00 00 00 00 00 00 00 00 d0")
        self.assertEqual(dp.parse_aula(charging), (96, True))
        invalid = bytearray(charging)
        invalid[6] = 2
        invalid[-1] = sum(invalid[:-1]) & 255
        self.assertIsNone(dp.parse_aula(invalid))

    def test_recorded_replies(self):
        aula = bytes.fromhex("13 0a 01 00 04 05 42 01 00 00 00 00 00 00 00 00 00 00 00 6a")
        rapoo = bytes.fromhex("01 00 34").ljust(33, b"\0")
        moon = bytes.fromhex("4b 80 27 06 00 00 00 00 01 5a").ljust(64, b"\0")
        self.assertEqual(dp.parse_aula(aula), (66, False))
        self.assertEqual(dp.parse_rapoo(rapoo), (52, False))
        self.assertEqual(dp.parse_moondrop(moon), 90)
        for parser, good, level_offset in ((dp.parse_aula, aula, 6),
                                           (dp.parse_rapoo, rapoo, 2),
                                           (dp.parse_moondrop, moon, 9)):
            for bad in (b"", good[:-1], b"\0" * len(good)):
                self.assertIsNone(parser(bad))
            bad = bytearray(good)
            bad[level_offset] = 101
            if parser == dp.parse_aula:
                bad[-1] = sum(bad[:-1]) & 255
            self.assertIsNone(parser(bad))

    def test_zero_is_valid_but_absent_device_is_unknown(self):
        self.assertEqual(dp.parse_rapoo(b"\x01\x00\x00".ljust(33, b"\0")), (0, False))
        moon = bytearray.fromhex("4b 80 27 06 00 00 00 00 01 00")
        self.assertEqual(dp.parse_moondrop(bytes(moon).ljust(64, b"\0")), 0)
        moon[8] = 0
        self.assertIsNone(dp.parse_moondrop(bytes(moon).ljust(64, b"\0")))

    def test_rapoo_busy_then_reply_and_sleep_is_not_zero(self):
        from unittest.mock import Mock
        device, feature = Mock(), Mock()
        feature.get_feature_report.side_effect = [
            b"\x02".ljust(33, b"\0"), b"\x01\x00\x34".ljust(33, b"\0")]
        with patch.object(dp.time, "sleep"):
            self.assertEqual(dp.read_rapoo(device, feature), (52, False))
        device.write_output.assert_called_once_with(b"\x06\xa5\xaa".ljust(33, b"\0"), use_hidd=False)
        feature.get_feature_report.side_effect = [b"\x00\xa5\xaa".ljust(33, b"\0")]
        with patch.object(dp.time, "sleep"):
            self.assertIsNone(dp.read_rapoo(device, feature))

    def test_moondrop_does_not_query_battery_when_headset_is_off(self):
        from unittest.mock import Mock
        device = Mock()
        with patch.object(dp, "exchange", return_value=b"\x4b\x80\x31\x01\x00".ljust(64, b"\0")) as query:
            self.assertIsNone(dp.read_moondrop(device))
            self.assertEqual(query.call_count, 1)

    def test_wrong_aula_checksum_and_identity_rejected(self):
        status = bytes.fromhex("13 0a 01 00 04 05 42 01 00 00 00 00 00 00 00 00 00 00 00 6a")
        self.assertIsNone(dp.parse_aula(status[:-1] + b"\0"))
        identity = bytes.fromhex("13 05 01 00 0a 03 00 00 00 01 58 01 00 00 00 00 00 00 00 80")
        battery = bytearray.fromhex("13 4a 01 00 02 42 01 00 00 00 00 00 00 00 00 00 00 00 00 00")
        battery[-1] = sum(battery[:-1]) & 255
        class Device:
            def __init__(self, replies):
                self.replies = iter(replies)
            def drain(self): pass
            def write_output(self, packet, use_hidd):
                self.packet = packet
            def read_input(self, timeout_ms):
                return next(self.replies, None)
        dev = Device([identity, bytes(battery)])
        self.assertEqual(dp.read_aula(dev), (66, False))
        self.assertEqual(bytes(dev.packet[:2]), b"\x13\x4a")
        self.assertEqual(sum(dev.packet[:-1]) & 255, dev.packet[-1])
        with patch.object(dp.time, "monotonic", side_effect=[0, 0.1, 0.2, 2, 2.1, 3.5]), \
             patch.object(dp.time, "sleep"):
            self.assertIsNone(dp.read_aula(Device([status])))

    def test_only_exact_interfaces_are_selected(self):
        for key, spec in dp.INTERFACES.items():
            vid, pid, fragment, page, usage, size_in, size_out, size_feature = spec
            item = SimpleNamespace(vid=vid, pid=pid, path=f"hid#{fragment}", usage_page=page,
                                   usage=usage, input_len=size_in, output_len=size_out,
                                   feature_len=size_feature)
            self.assertIs(dp.find_interface([item], key), item)
            for field in ("vid", "pid", "usage", "usage_page", "input_len", "output_len", "feature_len"):
                bad = SimpleNamespace(**vars(item))
                setattr(bad, field, getattr(bad, field) + 1)
                self.assertIsNone(dp.find_interface([bad], key))
            item.path = "hid#mi_99&col01"
            self.assertIsNone(dp.find_interface([item], key))

    def test_missing_receivers_do_not_open_any_device(self):
        with patch.object(dp.hw, "HidDevice") as opened:
            for key in ("mouse", "keyboard", "headset"):
                result = dp.read_device(key, [])
                self.assertIsNone(result.level)
                self.assertTrue(result.error)
                self.assertEqual(result.connection, "absent")
            opened.assert_not_called()

    def test_mouse_external_charger_transition_does_not_become_offline(self):
        # Three actual captures: unplugged 52%, external charger 52%, unplugged 53%.
        for packet, expected in (("01 00 34", (52, False)), ("01 02 34", (52, True)),
                                 ("01 00 35", (53, False))):
            self.assertEqual(dp.parse_rapoo(bytes.fromhex(packet).ljust(33, b"\0")), expected)
        for packet in ("02 02 34", "00 02 34", "01 03 34", "01 02 7f"):
            self.assertIsNone(dp.parse_rapoo(bytes.fromhex(packet).ljust(33, b"\0")))

    def test_charging_state_byte_is_distinct_from_busy_ack_byte(self):
        from unittest.mock import Mock
        device, feature = Mock(), Mock()
        feature.get_feature_report.side_effect = [b"\x02".ljust(33, b"\0"),
                                                  b"\x01\x02\x34".ljust(33, b"\0")]
        with patch.object(dp.time, "sleep"):
            self.assertEqual(dp.read_rapoo(device, feature), (52, True))

    def test_feature_success_with_unrelated_payload_is_not_a_battery_reply(self):
        unrelated = bytearray(b"\x01\x00\x30".ljust(33, b"\0"))
        unrelated[4] = 1
        self.assertIsNone(dp.parse_rapoo(unrelated))

    def test_device_mapping_keeps_external_power_distinct_from_actual_charging(self):
        from unittest.mock import MagicMock
        interface = SimpleNamespace(path="test")
        for key, status in (("mouse", (52, True)), ("keyboard", (85, True))):
            opened = MagicMock()
            opened.__enter__.return_value = opened
            opened.write_access = True
            reader = "read_rapoo" if key == "mouse" else "read_aula"
            with patch.object(dp, "find_interface", return_value=interface), \
                 patch.object(dp.hw, "HidDevice", return_value=opened), \
                 patch.object(dp, reader, return_value=status):
                result = dp.read_device(key, [])
            self.assertEqual(result.connection, "online")
            self.assertEqual(result.level, status[0])
            self.assertTrue(result.external_power)
            self.assertIs(result.charging, True if key == "mouse" else None)


if __name__ == "__main__":
    unittest.main()

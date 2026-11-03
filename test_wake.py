import ctypes
import unittest
from ctypes import wintypes
from unittest.mock import MagicMock, patch

from core import Reading
from wake_monitor import RawInputHeader, WakeGate, WakeMonitor, device_kind


class WakeTests(unittest.TestCase):
    def test_healthy_device_activity_does_not_add_queries(self):
        gate = WakeGate()
        for instant in (0, 0.1, 0.2, 2, 10, 31, 60, 120):
            self.assertFalse(gate.observe("mouse", now=instant))

    def test_offline_device_input_bypasses_idle_requirement(self):
        gate = WakeGate()
        self.assertTrue(gate.observe("keyboard", missing=True, now=0))
        self.assertFalse(gate.observe("keyboard", missing=True, now=6.1))
        self.assertTrue(gate.observe("keyboard", missing=True, now=60))

    def test_own_headset_replies_do_not_refresh_forever(self):
        gate = WakeGate()
        self.assertFalse(gate.observe("headset", missing=True, polling=True, now=0))
        self.assertTrue(gate.observe("headset", missing=True, polling=False, now=6))

    def test_no_device_input_queues_another_query_while_worker_is_active(self):
        gate = WakeGate()
        for key in ("mouse", "keyboard", "headset"):
            self.assertFalse(gate.observe(key, missing=True, polling=True, now=0))

    def test_only_our_devices_match(self):
        self.assertEqual(device_kind(r"\\?\hid#VID_24AE&PID_1416", 0), "mouse")
        self.assertEqual(device_kind(r"\\?\hid#VID_3554&PID_FA09", 1), "keyboard")
        self.assertEqual(device_kind(r"\\?\hid#VID_35D8&PID_0133", 2), "headset")
        self.assertIsNone(device_kind(r"\\?\hid#VID_0B05&PID_19AF", 2))
        self.assertIsNone(device_kind(r"\\?\hid#VID_3554&PID_FA09", 0))

    def test_native_header_event_and_resume_reach_callback(self):
        api, app, callback = MagicMock(), MagicMock(), MagicMock()
        api.RegisterRawInputDevices.return_value = True
        with patch("wake_monitor.ctypes.WinDLL", return_value=api):
            monitor = WakeMonitor(app, 123, callback,
                lambda: {"keyboard": Reading("keyboard", "", "", None)}, lambda: False)
        def header_only(handle, command, output, size, header_size):
            self.assertEqual(command, 0x10000005)  # RID_HEADER, never RID_INPUT.
            header = ctypes.cast(output, ctypes.POINTER(RawInputHeader)).contents
            header.kind, header.device = 1, 456
            return ctypes.sizeof(RawInputHeader)
        api.GetRawInputData.side_effect = header_only
        monitor.lookup = lambda handle, raw_type=None: "keyboard"
        message = wintypes.MSG()
        message.message, message.lParam = 0xFF, 789
        self.assertEqual(monitor.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(message)), (False, 0))
        callback.assert_called_once()
        callback.reset_mock()
        message.message, message.wParam = 0x218, 18
        monitor.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(message))
        callback.assert_called_once()
        callback.reset_mock()
        message.message, message.wParam = 0x219, 7
        monitor.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(message))
        callback.assert_called_once()
        monitor.close()
        app.removeNativeEventFilter.assert_called_once_with(monitor)


if __name__ == "__main__":
    unittest.main()

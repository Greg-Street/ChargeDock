import json
import subprocess
import sys
import tempfile
import unittest
from ctypes import sizeof
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import native_tray as nt
from activity import RawActivity
from core import AlertPolicy, LevelStability, MODELS, Reading, defaults, parse_readings
from polling import RetrySchedule


class NativeTests(unittest.TestCase):
    def host(self):
        host = nt.Host.__new__(nt.Host)
        host.args = SimpleNamespace(smoke_test=None)
        host.config = defaults()
        host.readings = {key: Reading(key, *value, level=50) for key, value in MODELS.items()}
        host.schedule = RetrySchedule(MODELS)
        host.policy = AlertPolicy()
        host.stability = LevelStability()
        host.stale, host.error, host.updated = set(), "", 0
        host.job, host.job_file, host.job_keys = None, None, ["headset"]
        host.nid = nt.NotifyIcon(size=sizeof(nt.NotifyIcon))
        host.publish = Mock()
        return host

    def test_host_imports_no_qt_in_fresh_interpreter(self):
        subprocess.run([sys.executable, "-c", "import sys,native_tray; assert not any(n.startswith('PySide6') for n in sys.modules)"],
                       cwd=Path(__file__).parent, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(sizeof(nt.NotifyIcon), 976)

    def test_launch_preserves_paths_with_spaces(self):
        with patch.object(nt.sys, "frozen", True, create=True):
            result = nt.launch_arguments("--data-dir", Path(r"D:\Program Files\Test Data"))
        self.assertEqual(result, [sys.executable, "--data-dir", r"D:\Program Files\Test Data"])

    def test_low_alert_owned_by_host_and_charging_is_excluded(self):
        host = self.host()
        host.job_keys = ["headset", "keyboard"]
        low = Reading("headset", *MODELS["headset"], level=10)
        charging = Reading("keyboard", *MODELS["keyboard"], level=10, charging=True)
        with patch.object(nt.shell32, "Shell_NotifyIconW", return_value=True) as notice:
            host.finish_reading([low, charging])
            host.finish_reading([low, charging])
            self.assertEqual(notice.call_count, 1)
            self.assertIn("10", host.nid.info)
        self.assertEqual(host.readings["mouse"].level, 50)

    def test_worker_error_keeps_previous_values_and_only_marks_queried_keys(self):
        host = self.host()
        with patch.object(nt.shell32, "Shell_NotifyIconW") as notice:
            host.finish_reading([], "timeout")
            notice.assert_not_called()
        self.assertEqual(host.stale, {"headset"})
        self.assertEqual(host.readings["headset"].level, 50)
        self.assertEqual(host.schedule.failures["headset"], 1)
        self.assertEqual(host.schedule.failures["mouse"], 0)

    def test_worker_timeout_terminates_only_that_worker(self):
        host = self.host()
        host.job = Mock()
        host.job.poll.return_value = None
        job = host.job
        host.exit_at, host.job_started = None, 0
        host.pending, host.panel_requested = set(), None
        host.ticking = False
        host.finish_reading, host.arm_timer = Mock(), Mock()
        host.update_icon = Mock()
        with patch.object(nt.time, "monotonic", return_value=10):
            host.tick()
        job.kill.assert_called_once()
        job.wait.assert_called_once_with(timeout=1)
        self.assertIn("超时", host.finish_reading.call_args.args[1])

    def test_temporary_no_reply_keeps_marked_last_reading_but_unplug_clears_it(self):
        host = self.host()
        temporary = Reading("headset", *MODELS["headset"], error="接收器已连接，设备未回复，请唤醒后刷新")
        host.finish_reading([temporary])
        self.assertEqual(host.readings["headset"].level, 50)
        self.assertEqual(host.stale, {"headset"})
        self.assertEqual(host.schedule.failures["headset"], 1)
        host.finish_reading([Reading("headset", *MODELS["headset"], level=55)])
        self.assertEqual(host.stale, set())
        self.assertEqual(host.readings["headset"].level, 55)
        host.finish_reading([Reading("headset", *MODELS["headset"], error="未找到对应的 2.4G 接收器",
                                     connection="absent")])
        self.assertIsNone(host.readings["headset"].level)

    def test_failed_query_preserves_percent_but_never_preserves_old_charging_state(self):
        host = self.host()
        host.job_keys = ["mouse"]
        host.readings["mouse"] = Reading("mouse", *MODELS["mouse"], level=52, charging=True,
                                         external_power=True, connection="online", updated=123)
        host.finish_reading([Reading("mouse", *MODELS["mouse"], error="未回复", connection="unresponsive")])
        current = host.readings["mouse"]
        self.assertEqual((current.level, current.updated), (52, 123))
        self.assertIsNone(current.charging)
        self.assertIsNone(current.external_power)
        self.assertEqual(current.connection, "unresponsive")
        host.finish_reading([], "worker failed")
        self.assertIsNone(host.readings["mouse"].charging)

    def test_reentrant_timer_cannot_consume_a_worker_result_twice(self):
        host = self.host()
        host.ticking = False
        host.tick_once = Mock(side_effect=lambda: host.tick())
        host.arm_timer = Mock()
        host.tick()
        host.tick_once.assert_called_once()
        host.arm_timer.assert_called_once()
        self.assertFalse(host.ticking)

    def test_missing_worker_file_reports_failure_and_does_not_stop_the_timer(self):
        host = self.host()
        host.exit_at, host.ticking = None, False
        host.pending, host.panel_requested = set(), None
        host.job = Mock()
        host.job.poll.return_value = 0
        host.schedule.due = Mock(return_value=[])
        host.arm_timer, host.update_icon = Mock(), Mock()
        with tempfile.TemporaryDirectory() as folder:
            host.job_file = Path(folder) / "missing.json"
            host.tick()
        self.assertIn("自动重试", host.error)
        self.assertNotIn("missing.json", host.error)
        self.assertEqual(host.schedule.failures["headset"], 1)
        host.arm_timer.assert_called_once()
        self.assertIsNone(host.readings["headset"].charging)

    def test_state_has_exactly_three_latest_readings_and_validates_levels(self):
        host = self.host()
        host.panel_point = None
        host.update_icon = Mock()
        with tempfile.TemporaryDirectory() as folder:
            host.directory = Path(folder)
            nt.Host.publish(host)
            data = json.loads((host.directory / "host-state.json").read_text(encoding="utf-8"))
        self.assertEqual([r.key for r in parse_readings(data["readings"])], list(MODELS))
        data["readings"][0]["level"] = 101
        with self.assertRaises(ValueError):
            parse_readings(data["readings"])

    def test_activity_device_kind_is_cached_between_events(self):
        observer = RawActivity.__new__(RawActivity)
        observer.devices = {123: "hid#VID_24AE&PID_1416"}
        observer.kinds = {}
        with patch("activity.device_kind", return_value="mouse") as kind:
            self.assertEqual(observer.lookup(123, 0), "mouse")
            self.assertEqual(observer.lookup(123, 0), "mouse")
            kind.assert_called_once()


if __name__ == "__main__":
    unittest.main()

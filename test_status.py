import unittest
from dataclasses import asdict

from core import AlertPolicy, MODELS, Reading, defaults, is_expired, parse_readings, reading_state
from polling import RetrySchedule


class StatusTests(unittest.TestCase):
    def test_external_power_suppresses_low_warning_without_claiming_charging(self):
        reading = Reading("keyboard", *MODELS["keyboard"], level=10, external_power=True,
                          connection="online")
        self.assertIsNone(reading.charging)
        self.assertEqual(reading_state(reading), "已接电源")
        self.assertEqual(AlertPolicy().evaluate([reading], defaults()), [])

    def test_charging_refresh_is_capped_without_altering_user_config(self):
        schedule = RetrySchedule(MODELS)
        charging = Reading("mouse", *MODELS["mouse"], level=52, charging=True)
        schedule.record([charging], ["mouse"], 300, now=100)
        self.assertEqual(schedule.deadlines["mouse"], 130)
        charging.charging = False
        schedule.record([charging], ["mouse"], 300, now=130)
        self.assertEqual(schedule.deadlines["mouse"], 430)

    def test_status_does_not_infer_connection_from_a_cached_percent(self):
        cached = Reading("mouse", *MODELS["mouse"], level=52, error="timeout", connection="unresponsive")
        self.assertEqual(reading_state(cached), "暂未回复 · 上次读数")
        cached.level, cached.connection = None, "absent"
        self.assertEqual(reading_state(cached), "接收器未连接")
        cached.connection = "unresponsive"
        self.assertEqual(reading_state(cached), "等待设备回复")

    def test_clock_jump_and_old_reading_expire(self):
        reading = Reading("mouse", *MODELS["mouse"], level=52, updated=1000)
        self.assertFalse(is_expired(reading, 300, now=1300))
        self.assertTrue(is_expired(reading, 300, now=1331))
        self.assertTrue(is_expired(reading, 300, now=900))

    def test_ipc_rejects_invalid_power_connection_and_time_fields(self):
        for field, invalid in (("charging", "false"), ("external_power", 1),
                               ("connection", "made-up"), ("updated", float("nan"))):
            values = [asdict(Reading(key, *model, level=50)) for key, model in MODELS.items()]
            values[0][field] = invalid
            with self.assertRaises(ValueError, msg=field):
                parse_readings(values)


if __name__ == "__main__":
    unittest.main()

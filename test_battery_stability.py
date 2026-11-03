import unittest
from dataclasses import asdict

from core import AlertPolicy, LevelStability, MODELS, Reading, defaults, parse_readings, reading_state
from polling import RetrySchedule


def sample(key, percent, powered=False, now=1000):
    return Reading(key, *MODELS[key], level=percent, charging=powered if key == "mouse" else None,
                   external_power=powered, connection="online", updated=now)


class BatteryStabilityTests(unittest.TestCase):
    def test_85_to_95_when_plugged_does_not_claim_ten_percent_was_charged(self):
        meter = LevelStability()
        self.assertEqual(meter.apply(sample("keyboard", 85), now=1000).level, 85)
        powered = meter.apply(sample("keyboard", 95, True), now=1001)
        self.assertIsNone(powered.level)
        self.assertEqual(powered.raw_level, 95)
        self.assertEqual(reading_state(powered), "已接电源")
        self.assertEqual(powered.level_quality, "powered")

    def test_unplugging_keeps_rebound_unconfirmed_until_three_stable_samples(self):
        meter = LevelStability()
        meter.apply(sample("keyboard", 95, True), now=1000)
        for now, level in ((1001, 91), (1031, 89), (1061, 85), (1091, 85)):
            current = meter.apply(sample("keyboard", level, now=now), now=now)
            self.assertIsNone(current.level)
            self.assertEqual(current.raw_level, level)
            self.assertEqual(current.level_quality, "settling")
        current = meter.apply(sample("keyboard", 85, now=1121), now=1121)
        self.assertEqual(current.level, 85)
        self.assertTrue(current.approximate)

    def test_mouse_53_to_48_to_53_does_not_present_the_outlier_as_charge_consumed(self):
        meter = LevelStability()
        self.assertEqual(meter.apply(sample("mouse", 53), now=1000).level, 53)
        outlier = meter.apply(sample("mouse", 48), now=1001)
        self.assertIsNone(outlier.level)
        self.assertEqual(outlier.raw_level, 48)
        self.assertEqual(reading_state(outlier), "读数波动 · 待确认")
        restored = meter.apply(sample("mouse", 53), now=1002)
        self.assertEqual(restored.level, 53)

    def test_real_change_is_accepted_without_clamping_to_the_old_level(self):
        meter = LevelStability()
        meter.apply(sample("mouse", 53), now=1000)
        for now in (1001, 1031):
            self.assertIsNone(meter.apply(sample("mouse", 48), now=now).level)
        self.assertEqual(meter.apply(sample("mouse", 48), now=1061).level, 48)

    def test_rapid_clicks_do_not_substitute_for_time_spaced_confirmation(self):
        meter = LevelStability()
        meter.apply(sample("mouse", 53), now=1000)
        for now in range(1001, 1011):
            self.assertIsNone(meter.apply(sample("mouse", 48), now=now).level)

    def test_no_reply_does_not_count_as_confirmation_and_removal_resets_history(self):
        meter = LevelStability()
        meter.apply(sample("mouse", 53), now=1000)
        meter.apply(sample("mouse", 48), now=1001)
        missing = Reading("mouse", *MODELS["mouse"], error="no reply", connection="unresponsive")
        self.assertEqual(meter.apply(missing, now=1031).connection, "unresponsive")
        self.assertIsNone(meter.apply(sample("mouse", 48), now=1061).level)
        absent = Reading("mouse", *MODELS["mouse"], error="absent", connection="absent")
        meter.apply(absent, now=1062)
        self.assertEqual(meter.apply(sample("mouse", 48), now=1063).level, 48)

    def test_unknown_power_after_failure_cannot_turn_a_powered_sample_into_discharge(self):
        meter = LevelStability()
        meter.apply(sample("mouse", 52, True), now=1000)
        failed = Reading("mouse", *MODELS["mouse"], connection="unresponsive", error="timeout")
        meter.apply(failed, now=1001)
        self.assertEqual(meter.apply(sample("mouse", 48), now=1002).level_quality, "settling")

    def test_low_raw_value_still_warns_during_confirmation(self):
        meter = LevelStability()
        meter.apply(sample("mouse", 53), now=1000)
        low = meter.apply(sample("mouse", 10), now=1001)
        warnings = AlertPolicy().evaluate([low], defaults())
        self.assertEqual([r.level for r in warnings], [10])
        charging = meter.apply(sample("mouse", 10, True), now=1002)
        self.assertEqual(AlertPolicy().evaluate([charging], defaults()), [])

    def test_online_power_only_and_unstable_samples_are_success_not_offline_retries(self):
        schedule = RetrySchedule(MODELS)
        meter = LevelStability()
        powered = meter.apply(sample("keyboard", 95, True), now=1000)
        schedule.record([powered], ["keyboard"], 300, now=50)
        self.assertEqual(schedule.failures["keyboard"], 0)
        self.assertEqual(schedule.deadlines["keyboard"], 80)

    def test_confirming_poll_rate_returns_to_user_interval_after_two_minutes(self):
        meter = LevelStability()
        meter.apply(sample("mouse", 53), now=1000)
        uncertain = meter.apply(sample("mouse", 48), now=1001)
        schedule = RetrySchedule(MODELS)
        schedule.record([uncertain], ["mouse"], 300, now=20, wall_now=1001)
        self.assertEqual(schedule.deadlines["mouse"], 80)
        schedule.record([uncertain], ["mouse"], 300, now=140, wall_now=1122)
        self.assertEqual(schedule.deadlines["mouse"], 440)

    def test_headset_percent_is_not_filtered(self):
        reading = sample("headset", 90)
        self.assertIs(LevelStability().apply(reading), reading)

    def test_sleeping_devices_start_at_thirty_seconds_instead_of_five(self):
        schedule = RetrySchedule(MODELS)
        sleeping = Reading("mouse", *MODELS["mouse"], error="no reply", connection="unresponsive")
        schedule.record([sleeping], ["mouse"], 300, now=100)
        self.assertEqual(schedule.deadlines["mouse"], 130)
        schedule.record([sleeping], ["mouse"], 300, now=130)
        self.assertEqual(schedule.deadlines["mouse"], 190)

    def test_quality_ipc_is_validated(self):
        for field, bad in (("raw_level", 101), ("raw_level", True), ("level_quality", "invented"),
                           ("quality_since", float("nan"))):
            values = [asdict(Reading(key, *model)) for key, model in MODELS.items()]
            values[0][field] = bad
            with self.assertRaises(ValueError):
                parse_readings(values)


if __name__ == "__main__":
    unittest.main()

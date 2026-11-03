import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import QApplication
from core import LevelStability, MODELS, Reading, atomic_json, load_config
from ui import Controller
import native_tray as nt


class NativePanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name)
        atomic_json(self.directory / "settings.json", {"interval": 300, "language": "zh-CN"})
        values = [Reading(key, *model, level=87 if key == "keyboard" else 52,
                          charging=key == "keyboard") for key, model in MODELS.items()]
        atomic_json(self.directory / "host-state.json", {
            "readings": [asdict(r) for r in values], "updated": time.time(),
            "stale": [], "snoozed_until": 0, "busy": False, "error": "", "panel_point": None})
        args = SimpleNamespace(data_dir=self.directory, snapshot=Path("test"), snapshot_theme="light",
                               background=True, smoke_test=None, panel=True)
        self.controller = Controller(self.app, args, QLocalServer())
        self.addCleanup(self.dispose)

    def dispose(self):
        self.controller.cleanup()
        self.controller.panel.deleteLater()
        self.app.processEvents()

    def test_native_panel_shows_charging_without_own_tray_or_worker(self):
        self.assertFalse(self.controller.tray.isVisible())
        self.assertIsNone(self.controller.wake_monitor)
        self.assertFalse(self.controller.poller.active)
        self.assertEqual(self.controller.panel.cards["keyboard"].percentage.text(), "87%")
        self.assertEqual(self.controller.panel.cards["keyboard"].state_text.text(), "正在充电")
        with patch.object(nt, "send_command", return_value=True) as command, \
             patch.object(self.controller.poller, "start") as start:
            self.controller.refresh()
            command.assert_called_once_with(self.directory, nt.REFRESH)
            start.assert_not_called()

    def test_settings_keep_existing_interval_and_notify_host(self):
        with patch.object(nt, "send_command", return_value=True) as command:
            self.controller.alert_setting("headset", "threshold", 35)
            command.assert_called_once_with(self.directory, nt.CONFIG)
        saved = load_config(self.directory / "settings.json")
        self.assertEqual(saved["interval"], 300)
        self.assertEqual(saved["alerts"]["headset"]["threshold"], 35)

    def test_hiding_native_panel_exits_its_process(self):
        with patch.object(self.app, "quit") as quit:
            self.controller.panel.show()
            self.app.processEvents()
            self.controller.panel.hide()
            self.app.processEvents()
            quit.assert_called_once()

    def test_keyboard_power_is_not_claimed_as_active_charging_and_headset_is_unknown(self):
        controller = self.controller
        keyboard = Reading("keyboard", *MODELS["keyboard"], level=85, external_power=True,
                           connection="online", updated=time.time())
        controller.panel.cards["keyboard"].render(keyboard, controller.config)
        self.assertEqual(controller.panel.cards["keyboard"].state_text.text(), "已接电源")
        self.assertIn("满电停充", controller.panel.cards["keyboard"].toolTip())
        headset = Reading("headset", *MODELS["headset"], level=90, connection="online")
        controller.panel.cards["headset"].render(headset, controller.config)
        self.assertIn("充电状态未知", controller.panel.cards["headset"].toolTip())

    def test_stopped_host_state_expires_without_a_file_change(self):
        controller = self.controller
        controller.readings["mouse"].updated = time.time()
        original_stamp = controller.native_stamp
        with patch("ui.time.time", return_value=time.time() + 400):
            controller.read_native_state()
        self.assertEqual(controller.native_stamp, original_stamp)
        self.assertIn("mouse", controller.stale_keys)
        self.assertEqual(controller.panel.cards["mouse"].state_text.text(), "暂未回复 · 上次读数")

    def test_powered_and_unstable_reports_show_state_and_raw_tooltip_without_a_percentage(self):
        controller, meter = self.controller, LevelStability()
        powered = meter.apply(Reading("keyboard", *MODELS["keyboard"], level=95, external_power=True,
                                     connection="online", updated=time.time()))
        card = controller.panel.cards["keyboard"]
        card.render(powered, controller.config)
        self.assertEqual(card.percentage.text(), "—")
        self.assertEqual(card.state_text.text(), "已接电源")
        self.assertIn("95%", card.toolTip())
        self.assertTrue(card.bar.isHidden())
        settling = meter.apply(Reading("keyboard", *MODELS["keyboard"], level=85, external_power=False,
                                      connection="online", updated=time.time()))
        card.render(settling, controller.config)
        self.assertEqual(card.percentage.text(), "—")
        self.assertEqual(card.state_text.text(), "拔线后稳定中")

    def test_theme_change_preserves_each_devices_independent_freshness(self):
        controller = self.controller
        controller.readings["keyboard"] = Reading("keyboard", *MODELS["keyboard"], level=85,
            charging=False, external_power=False, connection="online", updated=time.time())
        controller.stale_keys = {"mouse"}
        controller.stale = True
        controller.panel.apply_theme("dark")
        self.assertEqual(controller.panel.cards["keyboard"].state_text.text(), "电池供电")

    def test_panel_checks_every_thirty_seconds_only_while_visible(self):
        controller = self.controller
        controller.native_poll_at = 0
        with patch.object(controller, "refresh") as refresh:
            controller.read_native_state()
            refresh.assert_not_called()
            controller.panel.show()
            controller.read_native_state()
            refresh.assert_called_once()
            self.assertGreaterEqual(controller.native_poll_at, time.monotonic() + 29)
            controller.read_native_state()
            refresh.assert_called_once()


if __name__ == "__main__":
    unittest.main()

"""ChargeDock domain logic; no UI and no network access."""
from __future__ import annotations

import json
import math
import os
import time
from collections import deque
from dataclasses import asdict, dataclass, replace
from pathlib import Path


@dataclass
class Reading:
    key: str
    title: str
    model: str
    level: int | None = None
    charging: bool | None = None
    approximate: bool = False
    error: str = ""
    updated: float = 0
    external_power: bool | None = None
    connection: str = "unknown"
    raw_level: int | None = None
    level_quality: str = "reported"
    quality_since: float = 0

    @property
    def alert_level(self):
        return self.level if self.level is not None else self.raw_level


MODELS = {
    "mouse": ("鼠标", "2.4G Mouse"),
    "keyboard": ("键盘", "2.4G Keyboard"),
    "headset": ("耳机", "2.4G Headset"),
}


def reading_state(reading: Reading, stale=False) -> str:
    if reading.connection == "absent":
        return "接收器未连接"
    if stale or reading.error:
        return "暂未回复 · 上次读数" if reading.level is not None else "等待设备回复"
    if reading.charging is True:
        return "正在充电"
    if reading.external_power is True:
        return "已接电源"
    if reading.level_quality == "settling":
        return "拔线后稳定中"
    if reading.level_quality == "unstable":
        return "读数波动 · 待确认"
    if reading.level is not None:
        return "电池供电" if reading.charging is False else "已连接"
    return "正在读取"


def is_expired(reading: Reading, interval: int, now=None) -> bool:
    now = time.time() if now is None else now
    return bool(reading.updated and (now - reading.updated > max(60, interval + 30)
                                    or reading.updated > now + 30))


def merge_reading(previous: Reading, current: Reading) -> Reading:
    if current.error and current.connection != "absent":
        return replace(current, level=previous.level, updated=previous.updated,
                       charging=None, external_power=None, raw_level=previous.raw_level,
                       level_quality=previous.level_quality, quality_since=previous.quality_since)
    return current


class LevelStability:
    """Confirm device reports, without estimating cell capacity or inventing a charge curve."""
    def __init__(self):
        self.powered, self.accepted, self.pending = {}, {}, {}

    def apply(self, reading: Reading, now=None) -> Reading:
        key = reading.key
        if reading.connection == "absent":
            for values in (self.powered, self.accepted, self.pending):
                values.pop(key, None)
        if key not in ("mouse", "keyboard") or reading.error or reading.connection != "online":
            return reading
        now = time.time() if now is None else now
        raw = reading.raw_level if reading.raw_level is not None else reading.level
        result = replace(reading, raw_level=raw, approximate=True)
        powered = bool(reading.charging or reading.external_power)
        previous_power = self.powered.get(key, False)
        self.powered[key] = powered
        if powered:
            self.pending.pop(key, None)
            return replace(result, level=None, level_quality="powered")
        if raw is None:
            return result
        if previous_power:
            self.pending[key] = ("settling", now, deque(maxlen=3))
        baseline = self.accepted.get(key)
        if key not in self.pending and baseline is not None and abs(raw - baseline) >= 3:
            self.pending[key] = ("unstable", now, deque(maxlen=3))
        if key in self.pending:
            quality, since, samples = self.pending[key]
            returned = quality == "unstable" and baseline is not None and abs(raw - baseline) <= 1
            if not returned:
                if samples and now < samples[-1][0]:
                    samples.clear()
                    since = now
                    self.pending[key] = (quality, since, samples)
                samples.append((now, raw))
                levels = [level for _, level in samples]
                confirmed = (len(samples) == 3 and samples[-1][0] - samples[0][0] >= 60
                             and max(levels) - min(levels) <= 2)
                if not confirmed:
                    return replace(result, level=None, level_quality=quality, quality_since=since)
            self.pending.pop(key, None)
        self.accepted[key] = raw
        return replace(result, level=raw, level_quality="reported", quality_since=0)


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def defaults() -> dict:
    return {"interval": 60, "dismiss_seconds": 12, "sound": False, "language": "system",
            "alerts": {"mouse": {"enabled": True, "threshold": 20},
                       "keyboard": {"enabled": True, "threshold": 20},
                       "headset": {"enabled": True, "threshold": 20}}}


def validate_config(value) -> dict:
    result = defaults()
    if not isinstance(value, dict):
        return result
    from i18n import LANGUAGES
    if isinstance(value.get("language"), str) and value["language"] in LANGUAGES:
        result["language"] = value["language"]
    if value.get("interval") in (30, 60, 120, 300):
        result["interval"] = value["interval"]
    if value.get("dismiss_seconds") in (8, 12, 20, 30):
        result["dismiss_seconds"] = value["dismiss_seconds"]
    result["sound"] = value.get("sound") is True
    alerts = value.get("alerts", {})
    for key in MODELS:
        item = alerts.get(key, {}) if isinstance(alerts, dict) else {}
        if not isinstance(item, dict):
            continue
        result["alerts"][key]["enabled"] = item.get("enabled", True) is True
        threshold = item.get("threshold", 20)
        if type(threshold) is int:
            result["alerts"][key]["threshold"] = max(1, min(99, threshold))
    return result


def load_config(path: Path) -> dict:
    try:
        return validate_config(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return defaults()


class AlertPolicy:
    """One reminder per low-battery episode, with 5% recovery hysteresis."""
    def __init__(self):
        self.notified: set[str] = set()
        self.snoozed_until = 0.0

    def evaluate(self, readings: list[Reading], config: dict, now: float | None = None) -> list[Reading]:
        now = time.time() if now is None else now
        notifications = []
        for reading in readings:
            if reading.key not in MODELS:
                continue
            item = config["alerts"][reading.key]
            if not item["enabled"]:
                self.notified.discard(reading.key)
                continue
            if reading.error:
                continue
            threshold = item["threshold"]
            level = reading.alert_level
            if reading.charging or reading.external_power:
                self.notified.discard(reading.key)
                continue
            if level is None:
                continue
            if level > min(99, threshold + 5):
                self.notified.discard(reading.key)
                continue
            if now < self.snoozed_until:
                continue
            if level <= threshold and reading.key not in self.notified:
                notifications.append(replace(reading, level=level))
                self.notified.add(reading.key)
        return notifications


def read_all(keys=None) -> list[Reading]:
    import device_proto
    import hid_win as hw
    keys = list(MODELS) if keys is None else list(keys)
    if any(key not in MODELS for key in keys) or len(set(keys)) != len(keys):
        raise ValueError("Unknown or duplicate device keys")
    if not keys:
        return []
    try:
        interfaces = hw.enumerate_hid()
    except Exception as exc:
        return [Reading(key, *MODELS[key], error=f"暂时无法读取：{exc}") for key in keys]
    result = []
    for key in keys:
        try:
            reading = device_proto.read_device(key, interfaces)
        except Exception as exc:
            reading = Reading(key, *MODELS[key], error=f"暂时无法读取：{exc}")
        result.append(reading)
    return result


def poll_to_file(path: Path, key=None) -> int:
    keys = [key] if isinstance(key, str) else key
    atomic_json(path, [asdict(r) for r in read_all(keys)])
    return 0


def read_poll_file(path: Path, expected_keys=None) -> list[Reading]:
    return parse_readings(json.loads(path.read_text(encoding="utf-8")), expected_keys)


def parse_readings(raw, expected_keys=None) -> list[Reading]:
    expected_keys = list(MODELS) if expected_keys is None else expected_keys
    if not isinstance(raw, list) or len(raw) != len(expected_keys):
        raise ValueError("Incomplete device results")
    readings = [Reading(**item) for item in raw]
    if [r.key for r in readings] != expected_keys:
        raise ValueError("Unexpected devices")
    for reading in readings:
        for level in (reading.level, reading.raw_level):
            if level is not None and (type(level) is not int or not 0 <= level <= 100):
                raise ValueError("Invalid battery level")
        if any(value is not None and type(value) is not bool
               for value in (reading.charging, reading.external_power)):
            raise ValueError("Invalid power state")
        if reading.connection not in ("unknown", "online", "unresponsive", "absent"):
            raise ValueError("Invalid connection state")
        if reading.level_quality not in ("reported", "powered", "settling", "unstable"):
            raise ValueError("Invalid level quality")
        for stamp in (reading.updated, reading.quality_since):
            if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0:
                raise ValueError("Invalid reading time")
    return readings


def user_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "ChargeDockPersonal"

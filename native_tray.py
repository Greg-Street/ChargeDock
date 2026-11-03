"""Small Win32 tray host. Qt is loaded only by the disposable panel process.

Win32 declarations follow the original battery-viewer's tray_app.py and the
Windows SDK. The existing device readers, retry schedule and alert policy stay
shared with the Qt panel. No working-set trimming or forced GC is used.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import subprocess
import sys
import time
import uuid
from ctypes import wintypes as w
from dataclasses import asdict, replace
from pathlib import Path

from activity import RawActivity
from core import AlertPolicy, LevelStability, MODELS, Reading, atomic_json, is_expired, load_config, merge_reading, read_poll_file, reading_state, user_data_dir
from i18n import set_language, tr
from polling import RetrySchedule


ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
TRAY_MESSAGE, COMMAND_MESSAGE, PANEL_MESSAGE = 0x8001, 0x8002, 0x8003
SHOW, SETTINGS, REFRESH, SNOOZE, QUIT, CONFIG = range(1, 7)
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)


class WindowClass(ctypes.Structure):
    _fields_ = [("size", w.UINT), ("style", w.UINT), ("procedure", WNDPROC),
                ("class_extra", ctypes.c_int), ("window_extra", ctypes.c_int),
                ("instance", w.HINSTANCE), ("icon", w.HICON), ("cursor", w.HANDLE),
                ("background", w.HANDLE), ("menu", w.LPCWSTR), ("name", w.LPCWSTR),
                ("small_icon", w.HICON)]


class NotifyIcon(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("window", w.HWND), ("id", w.UINT),
                ("flags", w.UINT), ("callback", w.UINT), ("icon", w.HICON),
                ("tip", ctypes.c_wchar * 128), ("state", w.DWORD), ("state_mask", w.DWORD),
                ("info", ctypes.c_wchar * 256), ("version", w.UINT),
                ("info_title", ctypes.c_wchar * 64), ("info_flags", w.DWORD),
                ("guid", w.BYTE * 16), ("balloon_icon", w.HICON)]


def api(dll, name, result, *arguments):
    function = getattr(dll, name)
    function.restype, function.argtypes = result, list(arguments)
    return function


api(user32, "FindWindowW", w.HWND, w.LPCWSTR, w.LPCWSTR)
api(user32, "PostMessageW", w.BOOL, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
api(user32, "DefWindowProcW", ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
api(user32, "RegisterClassExW", w.ATOM, ctypes.POINTER(WindowClass))
api(user32, "RegisterWindowMessageW", w.UINT, w.LPCWSTR)
api(user32, "CreateWindowExW", w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID)
api(user32, "DestroyWindow", w.BOOL, w.HWND)
api(user32, "SetTimer", ctypes.c_size_t, w.HWND, ctypes.c_size_t, w.UINT, w.LPVOID)
api(user32, "KillTimer", w.BOOL, w.HWND, ctypes.c_size_t)
api(user32, "GetMessageW", ctypes.c_int, ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT)
api(user32, "TranslateMessage", w.BOOL, ctypes.POINTER(w.MSG))
api(user32, "DispatchMessageW", ctypes.c_ssize_t, ctypes.POINTER(w.MSG))
api(user32, "PostQuitMessage", None, ctypes.c_int)
api(user32, "LoadImageW", w.HANDLE, w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT)
api(user32, "DestroyIcon", w.BOOL, w.HICON)
api(user32, "CreatePopupMenu", w.HMENU)
api(user32, "AppendMenuW", w.BOOL, w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR)
api(user32, "TrackPopupMenu", w.UINT, w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.LPVOID)
api(user32, "DestroyMenu", w.BOOL, w.HMENU)
api(user32, "GetCursorPos", w.BOOL, ctypes.POINTER(w.POINT))
api(user32, "SetForegroundWindow", w.BOOL, w.HWND)
api(user32, "AllowSetForegroundWindow", w.BOOL, w.DWORD)
api(user32, "IsWindow", w.BOOL, w.HWND)
api(user32, "GetWindowThreadProcessId", w.DWORD, w.HWND, ctypes.POINTER(w.DWORD))
api(shell32, "Shell_NotifyIconW", w.BOOL, w.DWORD, ctypes.POINTER(NotifyIcon))
api(kernel32, "GetModuleHandleW", w.HMODULE, w.LPCWSTR)
api(kernel32, "CreateMutexW", w.HANDLE, w.LPVOID, w.BOOL, w.LPCWSTR)
api(kernel32, "CloseHandle", w.BOOL, w.HANDLE)


def window_class(directory):
    identity = hashlib.sha256(str(Path(directory).resolve()).lower().encode()).hexdigest()[:16]
    return "ChargeDockNative-" + identity


def send_command(directory, command):
    window = user32.FindWindowW(window_class(directory), None)
    return bool(window and user32.PostMessageW(window, COMMAND_MESSAGE, command, 0))


def launch_arguments(*arguments):
    return [sys.executable, *([] if getattr(sys, "frozen", False)
                             else [str(Path(__file__).with_name("chargedock.py"))]), *map(str, arguments)]


class Host:
    def __init__(self, args):
        self.args = args
        self.directory = args.data_dir or user_data_dir()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.config_path = self.directory / "settings.json"
        self.config = load_config(self.config_path)
        set_language(self.config["language"])
        self.readings = {key: Reading(key, *model) for key, model in MODELS.items()}
        self.schedule = RetrySchedule(MODELS)
        self.policy = AlertPolicy()
        self.stability = LevelStability()
        self.stale, self.error, self.updated = set(), "", 0
        self.pending, self.wake_due = set(), 0
        self.job, self.panel, self.activity = None, None, None
        self.job_file, self.job_keys, self.job_started = None, [], 0
        self.ticking = False
        self.window, self.closed, self.last_callback_error = None, False, ""
        self.panel_requested, self.panel_point = None, None
        self.exit_at = time.monotonic() + args.test_exit_ms / 1000 if args.test_exit_ms else None
        self.presentation = None
        self.icons = [user32.LoadImageW(None, str(ROOT / "assets" / name), 1, 32, 32, 0x10)
                      for name in ("tray.ico", "tray-warning.ico")]
        if not all(self.icons):
            raise ctypes.WinError(ctypes.get_last_error())
        self.procedure = WNDPROC(self.window_proc)
        self.class_name = window_class(self.directory)
        instance = kernel32.GetModuleHandleW(None)
        cls = WindowClass(size=ctypes.sizeof(WindowClass), procedure=self.procedure,
                          instance=instance, name=self.class_name)
        if not user32.RegisterClassExW(ctypes.byref(cls)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.window = user32.CreateWindowExW(0, self.class_name, self.class_name, 0,
                                             0, 0, 0, 0, None, None, instance, None)
        if not self.window:
            raise ctypes.WinError(ctypes.get_last_error())
        self.taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        self.nid = NotifyIcon(size=ctypes.sizeof(NotifyIcon), window=self.window, id=1,
                              callback=TRAY_MESSAGE)
        self.activity = RawActivity(self.window, self.request_wake,
                                    lambda: self.readings, lambda: self.job is not None)
        self.update_icon(0)
        self.publish()
        self.tick()
        if not args.background and not args.smoke_test:
            self.show_panel()

    def update_icon(self, action=1):
        tip, low = [tr("电量岛 · ChargeDock")], False
        for key, reading in self.readings.items():
            stale = key in self.stale or is_expired(reading, self.config["interval"])
            level = (f"{'约 ' if reading.approximate else ''}{reading.level}% · "
                     if reading.level is not None else "")
            tip.append(f"{tr(reading.title)}: {level}{tr(reading_state(reading, stale))}")
            item = self.config["alerts"][key]
            low |= (reading.alert_level is not None and not reading.error and not stale
                    and not reading.charging and not reading.external_power
                    and item["enabled"] and reading.alert_level <= item["threshold"])
        signature = ("\n".join(tip)[:127], low)
        if action == 1 and signature == self.presentation:
            return
        self.nid.flags, self.nid.tip, self.nid.icon = 7, signature[0], self.icons[int(low)]
        shell32.Shell_NotifyIconW(action, ctypes.byref(self.nid))
        if action == 0:
            self.nid.version = 3
            shell32.Shell_NotifyIconW(4, ctypes.byref(self.nid))
        self.presentation = signature

    def publish(self):
        self.update_icon()
        atomic_json(self.directory / "host-state.json", {
            "readings": [asdict(r) for r in self.readings.values()], "updated": self.updated,
            "error": self.error, "stale": sorted(self.stale), "busy": self.job is not None,
            "snoozed_until": self.policy.snoozed_until, "panel_point": self.panel_point})

    def request_wake(self, key=None, delay=0.65):
        self.pending.update([key] if key in MODELS else MODELS)
        due = time.monotonic() + delay
        self.wake_due = min(self.wake_due, due) if self.wake_due else due
        self.arm_timer()

    def start_reading(self, keys):
        self.job_keys = keys
        self.job_file = self.directory / f"poll-{uuid.uuid4().hex}.json"
        arguments = ["--poll-output", str(self.job_file)]
        for key in keys:
            arguments.extend(("--poll-key", key))
        self.job_started = time.monotonic()
        try:
            self.job = subprocess.Popen(launch_arguments(*arguments), creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError:
            self.finish_reading([], "读取进程无法启动，请稍后重试")
        self.publish()

    def finish_reading(self, readings, error=""):
        self.job = None
        if self.job_file:
            self.job_file.unlink(missing_ok=True)
        self.error = error
        readings = [self.stability.apply(reading) for reading in readings]
        if error:
            self.stale.update(self.job_keys)
            for key in self.job_keys:
                self.readings[key] = replace(self.readings[key], charging=None, external_power=None,
                                             connection="unresponsive", error=error)
        for reading in readings:
            previous = self.readings[reading.key]
            if reading.error and reading.connection != "absent":
                self.stale.add(reading.key)
                self.readings[reading.key] = merge_reading(previous, reading)
            else:
                self.stale.discard(reading.key)
                self.readings[reading.key] = reading
        if any(not reading.error for reading in readings):
            self.updated = max(self.updated, *(r.updated for r in readings))
        self.schedule.record(readings, self.job_keys, self.config["interval"])
        warnings = self.policy.evaluate(readings, self.config)
        if warnings and not self.args.smoke_test:
            self.nid.flags = 0x10
            self.nid.info_title = tr("设备电量偏低")[:63]
            self.nid.info = ("\n".join(tr("{device}剩余 {level}%", device=tr(r.title),
                                      level=f"约{r.level}" if r.approximate else r.level)
                                      for r in warnings) + tr("，可以准备充电了。"))[:255]
            self.nid.info_flags = 2 | 0x10 | 0x80
            shell32.Shell_NotifyIconW(1, ctypes.byref(self.nid))
            if self.config["sound"]:
                import winsound
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        self.publish()
        if self.args.smoke_test:
            atomic_json(self.args.smoke_test, {
                "error": error, "devices": [asdict(r) for r in self.readings.values()],
                "tray_count": 1, "wake_registered": self.activity.registration_ok,
                "qt_loaded": any(n.startswith("PySide6") for n in sys.modules)})
            user32.PostMessageW(self.window, 0x10, 0, 0)

    def tick(self):
        # Shell calls can dispatch messages; never consume a worker result twice.
        if self.ticking:
            return
        self.ticking = True
        try:
            self.tick_once()
        finally:
            self.ticking = False
            self.arm_timer()

    def tick_once(self):
        now = time.monotonic()
        if self.exit_at is not None and now >= self.exit_at:
            user32.DestroyWindow(self.window)
            return
        if self.job:
            code = self.job.poll()
            if code is not None:
                readings, error = [], ""
                if code:
                    error = "读取进程异常退出，将自动重试"
                else:
                    try:
                        readings = read_poll_file(self.job_file, self.job_keys)
                    except (OSError, ValueError, TypeError):
                        error = "本次读取结果不完整，将自动重试"
                self.finish_reading(readings, error)
            elif now - self.job_started >= 9:
                self.job.kill()
                self.job.wait(timeout=1)
                self.finish_reading([], "设备响应超时，已停止本次读取；请唤醒设备后刷新")
        if self.job is None:
            if self.pending and now >= self.wake_due:
                self.schedule.reset(self.pending)
                self.pending.clear()
                self.wake_due = 0
            keys = self.schedule.due(now)
            if keys:
                self.start_reading(keys)
        if self.panel_requested is not None:
            window, _ = self.panel_window()
            if window or not self.panel or self.panel.poll() is not None:
                settings = self.panel_requested
                self.panel_requested = None
                self.show_panel(settings)
        self.update_icon()

    def arm_timer(self):
        if not self.window or self.closed:
            return
        delays = [100 if self.job else min(30000, self.schedule.delay_ms())]
        if self.pending and not self.job:
            delays.append(max(1, int((self.wake_due - time.monotonic()) * 1000)))
        if self.exit_at is not None:
            delays.append(max(1, int((self.exit_at - time.monotonic()) * 1000)))
        if self.panel_requested is not None:
            delays.append(100)
        if not user32.SetTimer(self.window, 1, min(delays), None):
            raise ctypes.WinError(ctypes.get_last_error())

    def panel_window(self):
        try:
            value = json.loads((self.directory / "panel-window.json").read_text(encoding="utf-8"))
            window, pid = int(value["window"]), int(value["pid"])
            actual = w.DWORD()
            user32.GetWindowThreadProcessId(window, ctypes.byref(actual))
            if user32.IsWindow(window) and actual.value == pid:
                return window, pid
        except (OSError, ValueError, TypeError, KeyError):
            pass
        return None, None

    def show_panel(self, settings=False, toggle=False):
        if not settings:
            self.request_wake(delay=0)
        point = w.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        self.panel_point = [point.x, point.y]
        self.publish()
        window, pid = self.panel_window()
        if window:
            user32.AllowSetForegroundWindow(pid)
            user32.PostMessageW(window, PANEL_MESSAGE, 1 if settings else 0 if toggle else 2, 0)
            return
        if self.panel and self.panel.poll() is None:
            self.panel_requested = settings
            self.arm_timer()
            return
        arguments = ["--panel", "--data-dir", str(self.directory)]
        if settings:
            arguments.append("--panel-settings")
        self.panel = subprocess.Popen(launch_arguments(*arguments), creationflags=subprocess.CREATE_NO_WINDOW)

    def command(self, command):
        if command in (SHOW, SETTINGS):
            self.show_panel(command == SETTINGS)
        elif command == REFRESH:
            self.request_wake(delay=0)
        elif command == SNOOZE:
            self.policy.snoozed_until = time.time() + 1800
            self.publish()
        elif command == CONFIG:
            previous = self.config
            self.config = load_config(self.config_path)
            set_language(self.config["language"])
            if previous["interval"] != self.config["interval"]:
                self.schedule.change_interval(self.config["interval"])
            self.publish()
        elif command == QUIT:
            user32.DestroyWindow(self.window)
        self.arm_timer()

    def context_menu(self):
        menu = user32.CreatePopupMenu()
        try:
            for command, title in ((SHOW, "显示电量"), (SETTINGS, "偏好设置"),
                                    (REFRESH, "立即刷新"), (SNOOZE, "暂停提醒 30 分钟")):
                user32.AppendMenuW(menu, 0, command, tr(title))
            user32.AppendMenuW(menu, 0x800, 0, None)
            user32.AppendMenuW(menu, 0, QUIT, tr("退出电量岛"))
            point = w.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(self.window)
            command = user32.TrackPopupMenu(menu, 0x100 | 2, point.x, point.y, 0, self.window, None)
            user32.PostMessageW(self.window, 0, 0, 0)
            self.command(command)
        finally:
            user32.DestroyMenu(menu)

    def window_proc(self, window, message, wparam, lparam):
        try:
            if message == TRAY_MESSAGE:
                if lparam in (0x202, 0x400, 0x401, 0x405):
                    self.show_panel(toggle=lparam != 0x405)
                elif lparam in (0x205, 0x7B):
                    self.context_menu()
                return 0
            if message == COMMAND_MESSAGE:
                self.command(wparam)
                return 0
            if message == 0x113:
                self.tick()
                return 0
            if message == 0x10:
                user32.DestroyWindow(window)
                return 0
            if message == 2:
                self.cleanup()
                user32.PostQuitMessage(0)
                return 0
            if message == 0x11:
                return 1
            if message == getattr(self, "taskbar_created", None):
                self.update_icon(0)
            if self.activity:
                self.activity.process(w.MSG(window, message, wparam, lparam))
        except Exception as exc:
            text = f"{type(exc).__name__}: {exc}"
            if text != self.last_callback_error:
                self.last_callback_error = text
                (self.directory / "native-error.log").write_text(text, encoding="utf-8")
        return user32.DefWindowProcW(window, message, wparam, lparam)

    def cleanup(self):
        if self.closed:
            return
        self.closed = True
        user32.KillTimer(self.window, 1)
        shell32.Shell_NotifyIconW(2, ctypes.byref(self.nid))
        if self.activity:
            self.activity.close()
        if self.job:
            self.job.kill()
            self.job.wait(timeout=1)
        if self.job_file:
            self.job_file.unlink(missing_ok=True)
        window, _ = self.panel_window()
        if window:
            user32.PostMessageW(window, 0x10, 0, 0)
        for icon in self.icons:
            user32.DestroyIcon(icon)

    def run(self):
        message = w.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        return 0


def run(args):
    directory = args.data_dir or user_data_dir()
    if args.quit:
        send_command(directory, QUIT)
        return 0
    mutex = kernel32.CreateMutexW(None, False, "Local\\" + window_class(directory))
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if ctypes.get_last_error() == 183:
            for _ in range(10):
                if send_command(directory, SHOW):
                    break
                time.sleep(0.05)
            return 0
        return Host(args).run()
    finally:
        kernel32.CloseHandle(mutex)

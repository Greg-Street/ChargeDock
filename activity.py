"""Device activity notifications. Reads RAWINPUTHEADER only, never key contents."""
from __future__ import annotations

import ctypes
import re
import time
from ctypes import wintypes



class WakeGate:
    def __init__(self, cooldown=60):
        self.cooldown = cooldown
        self.triggered = {}

    def observe(self, key, missing=False, polling=False, now=None):
        now = time.monotonic() if now is None else now
        # Healthy readings follow the user's interval; input is only a failed-link recovery hint.
        if not missing or polling:
            return False
        if now - self.triggered.get(key, -1e10) >= self.cooldown:
            self.triggered[key] = now
            return True
        return False


def device_kind(path, raw_type=None):
    match = re.search(r"vid_([0-9a-f]{4})&pid_([0-9a-f]{4})", path.lower())
    if not match:
        return None
    vid, pid = (int(value, 16) for value in match.groups())
    if (vid, pid) == (0x35D8, 0x0133):
        return "headset"
    if (vid, pid) == (0x24AE, 0x1416) and raw_type in (None, 0):
        return "mouse"
    if (vid, pid) == (0x3554, 0xFA09) and raw_type in (None, 1):
        return "keyboard"
    return None


class RawInputDevice(ctypes.Structure):
    _fields_ = [("page", wintypes.USHORT), ("usage", wintypes.USHORT),
                ("flags", wintypes.DWORD), ("target", wintypes.HWND)]


class RawInputHeader(ctypes.Structure):
    _fields_ = [("kind", wintypes.DWORD), ("size", wintypes.DWORD),
                ("device", wintypes.HANDLE), ("wparam", wintypes.WPARAM)]


class RawActivity:
    USAGES = ((1, 2), (1, 6), (0x000C, 1))

    def __init__(self, hwnd, callback, readings, polling):
        self.callback = callback
        self.readings, self.polling = readings, polling
        self.gate = WakeGate()
        self.devices = {}
        self.kinds = {}
        self.registration_ok = False
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RawInputDevice), wintypes.UINT, wintypes.UINT]
        self.user32.RegisterRawInputDevices.restype = wintypes.BOOL
        self.user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p,
                                               ctypes.POINTER(wintypes.UINT), wintypes.UINT]
        self.user32.GetRawInputData.restype = wintypes.UINT
        self.user32.GetRawInputDeviceInfoW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                                      ctypes.c_void_p, ctypes.POINTER(wintypes.UINT)]
        self.user32.GetRawInputDeviceInfoW.restype = wintypes.UINT
        devices = (RawInputDevice * len(self.USAGES))(
            *(RawInputDevice(page, usage, 0x2100, hwnd) for page, usage in self.USAGES))
        self.registration_ok = bool(self.user32.RegisterRawInputDevices(devices, len(devices), ctypes.sizeof(RawInputDevice)))

    def lookup(self, handle, raw_type=None):
        if not handle:
            return None
        cache_key = (handle, raw_type)
        if cache_key in self.kinds:
            return self.kinds[cache_key]
        if handle not in self.devices:
            count = wintypes.UINT()
            if self.user32.GetRawInputDeviceInfoW(handle, 0x20000007, None, ctypes.byref(count)) == 0xFFFFFFFF:
                return None
            name = ctypes.create_unicode_buffer(count.value + 1)
            if self.user32.GetRawInputDeviceInfoW(handle, 0x20000007, name, ctypes.byref(count)) == 0xFFFFFFFF:
                return None
            self.devices[handle] = name.value
        self.kinds[cache_key] = device_kind(self.devices[handle], raw_type)
        return self.kinds[cache_key]

    def process(self, msg):
        try:
            if msg.message == 0xFF:  # WM_INPUT; do not read the payload.
                header = RawInputHeader()
                size = wintypes.UINT(ctypes.sizeof(header))
                count = self.user32.GetRawInputData(msg.lParam, 0x10000005,
                    ctypes.byref(header), ctypes.byref(size), ctypes.sizeof(header))
                if count == 0xFFFFFFFF:
                    return
                key = self.lookup(header.device, header.kind)
                if key:
                    current = self.readings().get(key)
                    missing = current is None or current.connection != "online" or bool(current.error)
                    if self.gate.observe(key, missing, self.polling()):
                        self.callback(key)
            elif msg.message == 0xFE:  # WM_INPUT_DEVICE_CHANGE
                key = self.lookup(msg.lParam)
                if int(msg.wParam) == 2:
                    self.devices.pop(msg.lParam, None)
                    self.kinds = {k: v for k, v in self.kinds.items() if k[0] != msg.lParam}
                if key:
                    self.callback(key)
            elif msg.message == 0x218 and int(msg.wParam) in (7, 18):
                self.gate.triggered.clear()
                self.callback()
            elif msg.message == 0x219 and int(msg.wParam) == 7:  # DBT_DEVNODES_CHANGED
                # A removed RAWINPUT handle may no longer reveal its device name.
                self.callback()
        except (OSError, ValueError, TypeError):
            pass
        # Normal Windows/Qt input handling remains intact, including cleanup.

    def close(self):
        if self.registration_ok:
            devices = (RawInputDevice * len(self.USAGES))(
                *(RawInputDevice(page, usage, 1, None) for page, usage in self.USAGES))
            self.user32.RegisterRawInputDevices(devices, len(devices), ctypes.sizeof(RawInputDevice))

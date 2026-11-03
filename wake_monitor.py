"""Qt adapter for the shared Windows header-only activity observer."""
import ctypes
from ctypes import wintypes
from PySide6.QtCore import QAbstractNativeEventFilter
from activity import RawActivity, WakeGate, device_kind, RawInputDevice, RawInputHeader


class WakeMonitor(RawActivity, QAbstractNativeEventFilter):
    def __init__(self, app, hwnd, callback, readings, polling):
        QAbstractNativeEventFilter.__init__(self)
        RawActivity.__init__(self, hwnd, callback, readings, polling)
        self.app = app
        app.installNativeEventFilter(self)

    def nativeEventFilter(self, event_type, message):
        if bytes(event_type) in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            self.process(wintypes.MSG.from_address(int(message)))
        return False, 0

    def close(self):
        self.app.removeNativeEventFilter(self)
        RawActivity.close(self)

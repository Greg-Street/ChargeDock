"""Read-only battery queries for this computer's three 2.4 GHz receivers.

Protocol sources and the verified interface/capture details: PERSONAL_BUILD.md.
No lighting, key mapping, EQ, pairing, or firmware commands are sent.
"""
from __future__ import annotations

import ctypes
import time

import hid_win as hw
from core import MODELS, Reading


INTERFACES = {
    "keyboard": (0x3554, 0xFA09, "mi_01&col01", 0xFF02, 2, 20, 20, 0),
    "mouse": (0x24AE, 0x1416, "mi_01&col05", 0xFF00, 14, 33, 33, 0),
    "mouse_feature": (0x24AE, 0x1416, "mi_01&col06", 0xFF00, 15, 0, 0, 33),
    "headset": (0x35D8, 0x0133, "mi_04&col03", 0x000C, 1, 64, 64, 0),
}
AULA_IDENTITY = bytes.fromhex("03 00 00 00 01 58")


def find_interface(interfaces, key):
    vid, pid, fragment, page, usage, size_in, size_out, size_feature = INTERFACES[key]
    return next((i for i in interfaces if
                 (i.vid, i.pid, i.usage_page, i.usage, i.input_len, i.output_len, i.feature_len)
                 == (vid, pid, page, usage, size_in, size_out, size_feature)
                 and fragment in i.path.lower()), None)


def valid_aula_packet(data):
    return len(data) == 20 and data[0] == 0x13 and sum(data[:-1]) & 0xFF == data[-1]


def parse_aula(data):
    if not valid_aula_packet(data):
        return None
    if data[1] & 0x7F == 0x4A and data[2:5] == b"\x01\x00\x02":
        level, state = data[5], data[6]
    elif data[:5] == b"\x13\x0a\x01\x00\x04":
        level, state = data[6], data[7]
    else:
        return None
    if state not in (0x01, 0x10) or level > 100:
        return None
    return level, state == 0x10


def parse_rapoo(data):
    # HidD_GetFeature returns the ACK at byte 0 (not report ID 8) on this receiver.
    if (len(data) != 33 or data[0] != 1 or data[1] not in (0, 1, 2)
            or data[2] > 100 or any(data[3:])):
        return None
    # The mouse protocol reports 00 on battery and 02 on the external charger.
    return data[2], data[1] == 2


def parse_moondrop(data):
    if len(data) != 64 or data[:4] != b"\x4b\x80\x27\x06":
        return None
    # The official decoder uses payload[4] as validity, payload[5] as percent.
    if not data[8] or data[9] > 100:
        return None
    return data[9]


def exchange(device, packet, accepts, timeout=0.8):
    device.drain()
    device.write_output(packet, use_hidd=False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = device.read_input(timeout_ms=80)
        if data and accepts(data):
            return data
    return None


def read_aula(device):
    identity_packet, battery_packet = bytearray(20), bytearray(20)
    for packet, command in ((identity_packet, 0x05), (battery_packet, 0x4A)):
        packet[:2] = bytes((0x13, command))
        packet[-1] = sum(packet[:-1]) & 0xFF
    for attempt in range(2):
        if attempt:
            time.sleep(0.15)
        identity = exchange(device, identity_packet, lambda d: valid_aula_packet(d)
                            and d[:5] == b"\x13\x05\x01\x00\x0a"
                            and d[5:11] == AULA_IDENTITY and d[11] == 1, timeout=0.3)
        if not identity:
            continue
        # 0x0A is an unsolicited status; use the dedicated getter instead of waiting for it.
        reply = exchange(device, battery_packet, lambda d: valid_aula_packet(d)
                         and d[1] & 0x7F == 0x4A and d[2:5] == b"\x01\x00\x02", timeout=0.4)
        if reply:
            return parse_aula(reply)
    return None


def read_rapoo(device, feature):
    # 0xA5 routes a read to the wireless mouse; 0xAA is GET_BATTERY_LEVEL.
    device.write_output(b"\x06\xa5\xaa".ljust(33, b"\0"), use_hidd=False)
    deadline = time.monotonic() + 0.8
    while time.monotonic() < deadline:
        time.sleep(0.02)
        data = feature.get_feature_report(8, 33)
        if data and data[0] == 2:  # ACK byte 0 is busy; byte 1 == 2 means charging.
            continue
        return parse_rapoo(data)
    return None


def read_moondrop(device):
    ping = exchange(device, b"\x4b\x80\x31\x00".ljust(64, b"\0"),
                    lambda d: len(d) == 64 and d[:4] == b"\x4b\x80\x31\x01")
    if not ping or ping[4] != 1:
        return None
    reply = exchange(device, b"\x4b\x80\x27\x00".ljust(64, b"\0"),
                     lambda d: len(d) == 64 and d[:3] == b"\x4b\x80\x27")
    return parse_moondrop(reply) if reply else None


def read_device(key, interfaces):
    result = Reading(key, *MODELS[key])
    iface = find_interface(interfaces, key)
    feature = find_interface(interfaces, "mouse_feature") if key == "mouse" else None
    if iface is None or (key == "mouse" and feature is None):
        result.connection = "absent"
        result.error = "未找到对应的 2.4G 接收器"
        return result
    result.connection = "unresponsive"
    kernel = hw.kernel32
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
    mutex = kernel.CreateMutexW(None, False, f"Local\\ChargeDockPersonal_{key}")
    if not mutex:
        result.error = "无法创建设备读取锁"
        return result
    acquired = False
    try:
        acquired = kernel.WaitForSingleObject(mutex, 1000) in (0, 0x80)
        if not acquired:
            result.error = "设备正在被另一窗口读取，请稍后刷新"
            return result
        with hw.HidDevice(iface.path, write=True) as device:
            if not device.write_access:
                result.error = "电量接口被占用，请稍后重试"
                return result
            if key == "mouse":
                with hw.HidDevice(feature.path) as response:
                    status = read_rapoo(device, response)
                if status is not None:
                    result.level, result.charging = status
                    result.external_power = result.charging
            elif key == "keyboard":
                status = read_aula(device)
                if status is not None:
                    result.level, result.external_power = status
                    # 10 proves cable power, but cannot distinguish charging from full/idle.
                    result.charging = None if result.external_power else False
            else:
                result.level = read_moondrop(device)
        # shortcut: The headset's published battery protocol has no charging field.
        if result.level is None:
            result.error = "接收器已连接，设备未回复，请唤醒后刷新"
        else:
            result.connection = "online"
            result.updated = time.time()
            result.raw_level = result.level
    except OSError as exc:
        result.error = f"读取失败：{exc}"
    finally:
        if acquired:
            kernel.ReleaseMutex(mutex)
        kernel.CloseHandle(mutex)
    return result

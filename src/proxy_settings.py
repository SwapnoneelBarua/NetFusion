"""Careful, recoverable management of the current user's Windows proxy."""

from __future__ import annotations

import base64
import ctypes
import json
import sys
from ctypes import wintypes


REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
LEASE_KEY = "pendingSystemProxyLease"
PROXY_KEYS = ("ProxyEnable", "ProxyServer", "ProxyOverride")


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise OSError("Windows Data Protection API is only available on Windows.")
    crypt32 = ctypes.WinDLL("Crypt32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    input_storage = ctypes.create_string_buffer(data)
    input_blob = DATA_BLOB(
        len(data), ctypes.cast(input_storage, ctypes.POINTER(ctypes.c_byte))
    )
    output_blob = DATA_BLOB()
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if protect:
        function.argtypes = [
            ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR,
            ctypes.POINTER(DATA_BLOB), wintypes.LPVOID, wintypes.LPVOID,
            wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
        ]
        function.restype = wintypes.BOOL
        ok = function(
            ctypes.byref(input_blob), "NetFusion proxy recovery state",
            None, None, None, 0x1, ctypes.byref(output_blob),
        )
    else:
        function.argtypes = [
            ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(DATA_BLOB), wintypes.LPVOID, wintypes.LPVOID,
            wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
        ]
        function.restype = wintypes.BOOL
        description = wintypes.LPWSTR()
        ok = function(
            ctypes.byref(input_blob), ctypes.byref(description),
            None, None, None, 0x1, ctypes.byref(output_blob),
        )
        if description:
            local_free(description)
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        local_free(output_blob.pbData)


def _read_value(key, name):
    import winreg
    try:
        return winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return None


def _read_snapshot():
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_QUERY_VALUE) as key:
        return {name: _read_value(key, name) for name in PROXY_KEYS}


def _set_values(values):
    import winreg
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_SET_VALUE
    ) as key:
        for name, entry in values.items():
            if entry is None:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            else:
                value, value_type = entry
                winreg.SetValueEx(key, name, 0, value_type, value)


def _notify_windows():
    if sys.platform != "win32":
        return
    try:
        wininet = ctypes.WinDLL("Wininet.dll", use_last_error=True)
        notify = wininet.InternetSetOptionW
        notify.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD]
        notify.restype = wintypes.BOOL
        notify(None, 39, None, 0)  # INTERNET_OPTION_SETTINGS_CHANGED
        notify(None, 37, None, 0)  # INTERNET_OPTION_REFRESH
    except (AttributeError, OSError):
        pass


def _save_encrypted(settings, payload: dict) -> None:
    encoded = base64.b64encode(_dpapi(json.dumps(payload).encode("utf-8"), True)).decode("ascii")
    settings.setValue(LEASE_KEY, encoded)
    settings.sync()
    if settings.status() != settings.Status.NoError:
        settings.remove(LEASE_KEY)
        raise OSError("Could not securely save the proxy recovery record.")


def recover_pending_system_proxy(settings) -> tuple[bool, str]:
    """Recover an interrupted NetFusion takeover, preserving newer settings."""
    encoded = settings.value(LEASE_KEY, "", type=str)
    if not encoded:
        return False, ""
    try:
        payload = json.loads(_dpapi(base64.b64decode(encoded, validate=True), False))
        previous = {
            name: tuple(entry) if entry is not None else None
            for name, entry in payload["previous"].items()
        }
        applied = {
            name: tuple(entry) if entry is not None else None
            for name, entry in payload["applied"].items()
        }
        import winreg
        changed = {}
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE
        ) as key:
            for name in PROXY_KEYS:
                current = _read_value(key, name)
                if current == applied.get(name):
                    changed[name] = previous.get(name)
            for name, entry in changed.items():
                if entry is None:
                    try:
                        winreg.DeleteValue(key, name)
                    except FileNotFoundError:
                        pass
                else:
                    value, value_type = entry
                    winreg.SetValueEx(key, name, 0, value_type, value)
        _notify_windows()
        settings.remove(LEASE_KEY)
        settings.sync()
        if len(changed) == len(PROXY_KEYS):
            return True, "Recovered the previous Windows proxy settings after an interrupted Boost session."
        return bool(changed), "Restored NetFusion-owned proxy values; values changed by another app were left alone."
    except Exception as exc:
        return False, f"Could not recover a saved Windows proxy state: {exc}"


class SystemProxyLease:
    """Temporarily directs WinINet HTTP/HTTPS traffic to NetFusion's loopback proxy."""

    def __init__(self, settings):
        self.settings = settings
        self._managed = False

    def acquire(self) -> str:
        """Return 'managed' or 'manual'; never take over another active proxy."""
        if sys.platform != "win32":
            raise OSError("System proxy takeover is supported only on Windows.")
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_QUERY_VALUE
        ) as key:
            proxy_enable = _read_value(key, "ProxyEnable")
            proxy_server = _read_value(key, "ProxyServer")
            auto_config = _read_value(key, "AutoConfigURL")
            auto_detect = _read_value(key, "AutoDetect")

        active_manual = bool(proxy_enable and proxy_enable[0]) and bool(proxy_server and proxy_server[0])
        active_pac = bool(auto_config and auto_config[0])
        active_auto_detect = bool(auto_detect and auto_detect[0])
        if active_manual or active_pac or active_auto_detect:
            return "manual"

        previous = _read_snapshot()
        old_override = previous.get("ProxyOverride")
        override_text = old_override[0] if old_override and isinstance(old_override[0], str) else ""
        bypass = [token.strip() for token in override_text.split(";") if token.strip()]
        for local in ("localhost", "127.0.0.1", "::1"):
            if local not in bypass:
                bypass.append(local)
        desired = {
            "ProxyEnable": (1, winreg.REG_DWORD),
            "ProxyServer": ("127.0.0.1:10801", winreg.REG_SZ),
            "ProxyOverride": (";".join(bypass), winreg.REG_SZ),
        }
        payload = {"previous": previous, "applied": desired}
        _save_encrypted(self.settings, payload)
        applied = {}
        try:
            for name, entry in desired.items():
                _set_values({name: entry})
                applied[name] = entry
            _notify_windows()
        except Exception:
            if applied:
                _set_values({
                    name: previous.get(name)
                    for name, entry in applied.items()
                    if _read_snapshot().get(name) == entry
                })
            self.settings.remove(LEASE_KEY)
            self.settings.sync()
            raise
        self._managed = True
        return "managed"

    def release(self) -> tuple[bool, str]:
        self._managed = False
        return recover_pending_system_proxy(self.settings)

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))

import proxy_settings  # noqa: E402


class FakeKey:
    def __init__(self, values):
        self.values = values

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeWinreg(types.ModuleType):
    HKEY_CURRENT_USER = object()
    KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1
    REG_DWORD = 4

    def __init__(self, values=None):
        super().__init__("winreg")
        self.values = dict(values or {})

    def OpenKey(self, _root, _path, _reserved, _access):
        return FakeKey(self.values)

    def QueryValueEx(self, key, name):
        if name not in key.values:
            raise FileNotFoundError(name)
        return key.values[name]

    def SetValueEx(self, key, name, _reserved, value_type, value):
        key.values[name] = (value, value_type)

    def DeleteValue(self, key, name):
        if name not in key.values:
            raise FileNotFoundError(name)
        del key.values[name]


class FakeSettings:
    class Status:
        NoError = 0

    def __init__(self):
        self.values = {}

    def setValue(self, name, value):
        self.values[name] = value

    def value(self, name, default="", type=None):
        return self.values.get(name, default)

    def remove(self, name):
        self.values.pop(name, None)

    def sync(self):
        pass

    def status(self):
        return self.Status.NoError


@unittest.skipUnless(sys.platform == "win32", "Windows registry/DPAPI behavior is Windows-only")
class ProxyLeaseTests(unittest.TestCase):
    def setUp(self):
        self.registry = FakeWinreg({"ProxyOverride": ("intranet", FakeWinreg.REG_SZ)})
        self.settings = FakeSettings()
        self.patches = [
            patch.dict(sys.modules, {"winreg": self.registry}),
            patch.object(proxy_settings, "_notify_windows"),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()

    def test_managed_proxy_is_restored_on_release(self):
        original = dict(self.registry.values)
        lease = proxy_settings.SystemProxyLease(self.settings)
        self.assertEqual(lease.acquire(), "managed")
        self.assertEqual(self.registry.values["ProxyServer"], ("127.0.0.1:10801", FakeWinreg.REG_SZ))

        restored, _message = lease.release()

        self.assertTrue(restored)
        self.assertEqual(self.registry.values, original)

    def test_recovery_restores_only_values_still_owned_by_netfusion(self):
        lease = proxy_settings.SystemProxyLease(self.settings)
        lease.acquire()
        self.registry.values["ProxyServer"] = ("another-app:8080", FakeWinreg.REG_SZ)

        restored, message = proxy_settings.recover_pending_system_proxy(self.settings)

        self.assertTrue(restored)
        self.assertEqual(self.registry.values["ProxyServer"], ("another-app:8080", FakeWinreg.REG_SZ))
        self.assertIn("left alone", message)
        self.assertNotIn(proxy_settings.LEASE_KEY, self.settings.values)

    def test_existing_manual_proxy_is_preserved(self):
        self.registry.values.update({
            "ProxyEnable": (1, FakeWinreg.REG_DWORD),
            "ProxyServer": ("existing-proxy:8080", FakeWinreg.REG_SZ),
        })
        original = dict(self.registry.values)

        result = proxy_settings.SystemProxyLease(self.settings).acquire()

        self.assertEqual(result, "manual")
        self.assertEqual(self.registry.values, original)
        self.assertNotIn(proxy_settings.LEASE_KEY, self.settings.values)

    def test_pac_configuration_is_preserved(self):
        self.registry.values["AutoConfigURL"] = ("https://proxy.example/pac", FakeWinreg.REG_SZ)
        original = dict(self.registry.values)

        result = proxy_settings.SystemProxyLease(self.settings).acquire()

        self.assertEqual(result, "manual")
        self.assertEqual(self.registry.values, original)


if __name__ == "__main__":
    unittest.main()

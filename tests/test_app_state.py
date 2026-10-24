from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtWidgets import QApplication, QSystemTrayIcon  # noqa: E402
from main import MainWindow, connection_kind  # noqa: E402


class FakeEngine:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class AdapterStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.refresh_patch = patch.object(MainWindow, "refresh", lambda _self: None)
        self.refresh_patch.start()
        self.window = MainWindow()

    def tearDown(self):
        self.window._tray_exit = True
        self.window.close()
        self.refresh_patch.stop()
        self.app.processEvents()

    def sample_adapters(self):
        return [
            {
                "Name": "Ethernet 2",
                "InterfaceDescription": "Remote NDIS based Internet Sharing Device",
                "Status": "Up",
                "LinkSpeed": "426 Mbps",
                "ifIndex": 16,
                "HardwareInterface": True,
                "IPv4Address": "10.99.26.9",
                "IPv4Gateway": "10.99.26.77",
            },
            {
                "Name": "Wi-Fi",
                "InterfaceDescription": "MediaTek Wi-Fi 6 MT7921 Wireless LAN Card",
                "Status": "Up",
                "LinkSpeed": "866 Mbps",
                "ifIndex": 6,
                "HardwareInterface": True,
                "IPv4Address": "192.168.1.5",
                "IPv4Gateway": "192.168.1.1",
            },
        ]

    def activate_fake_boost(self, adapters):
        self.window._boost_paths = []
        self.window.apply_snapshot(adapters, [])
        engine = FakeEngine()
        self.window.proxy_engine = engine
        self.window.boost_active = True
        self.window.system_proxy_managed = False
        self.window._active_path_signature = self.window._path_signature(self.window._boost_paths)
        return engine

    def test_remote_ndis_tether_is_detected_as_ethernet_and_warp_is_excluded(self):
        adapters = self.sample_adapters()
        self.assertEqual(connection_kind(adapters[0]), "Ethernet")
        self.assertEqual(connection_kind(adapters[1]), "Wi-Fi")
        self.assertIsNone(connection_kind({
            "Name": "CloudflareWARP",
            "InterfaceDescription": "Cloudflare WARP Interface",
            "Status": "Up",
            "HardwareInterface": False,
        }))

    def test_startup_remains_passive_and_detects_both_ready_paths(self):
        self.window.apply_snapshot(self.sample_adapters(), [])

        self.assertTrue(self.window._eligible)
        self.assertFalse(self.window.boost_active)
        self.assertEqual(self.window._ready_kinds, {"Ethernet", "Wi-Fi"})

    def test_adapter_loss_stops_proxy(self):
        adapters = self.sample_adapters()
        engine = self.activate_fake_boost(adapters)
        adapters[1]["Status"] = "Disconnected"
        adapters[1]["IPv4Address"] = ""
        adapters[1]["IPv4Gateway"] = ""

        self.window.apply_snapshot(adapters, [])

        self.assertTrue(engine.stopped)
        self.assertFalse(self.window.boost_active)

    def test_adapter_identity_change_stops_proxy(self):
        adapters = self.sample_adapters()
        engine = self.activate_fake_boost(adapters)
        adapters[1]["IPv4Address"] = "192.168.1.99"

        self.window.apply_snapshot(adapters, [])

        self.assertTrue(engine.stopped)
        self.assertFalse(self.window.boost_active)

    def test_warp_activation_stops_proxy(self):
        adapters = self.sample_adapters()
        engine = self.activate_fake_boost(adapters)
        adapters.append({
            "Name": "CloudflareWARP",
            "InterfaceDescription": "Cloudflare WARP Interface",
            "Status": "Up",
            "HardwareInterface": False,
        })

        self.window.apply_snapshot(adapters, [])

        self.assertTrue(engine.stopped)
        self.assertFalse(self.window.boost_active)

    def test_close_stops_boost_even_without_a_system_tray(self):
        adapters = self.sample_adapters()
        engine = self.activate_fake_boost(adapters)
        with patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
            self.window.close()

        self.assertTrue(engine.stopped)
        self.assertFalse(self.window.boost_active)


if __name__ == "__main__":
    unittest.main()

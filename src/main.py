"""NetFusion monitor with an on-demand local per-connection proxy."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QLockFile, QSettings, QStandardPaths, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QFont, QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPushButton, QSystemTrayIcon, QMenu, QVBoxLayout, QWidget,
    QScrollArea,
)
from proxy_engine import AdapterPath, HTTP_PROXY_PORT, HTTP_PROXY_HOST, SOCKS_PROXY_PORT, LocalProxyEngine
from proxy_settings import SystemProxyLease, recover_pending_system_proxy

APP_NAME = "NetFusion"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def app_version():
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    try:
        return (bundle_root / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "development"


APP_VERSION = app_version()


def app_icon():
    """Load the provided icon from source or the PyInstaller bundle."""
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return QIcon(str(bundle_root / "icon" / "icon.png"))


class SnapshotThread(QThread):
    snapshot_ready = Signal(object, object)

    def run(self):
        self.snapshot_ready.emit(*get_network_snapshot())


def run_powershell(command):
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=6,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def get_network_snapshot():
    """Fetch adapter state, IPv4 egress details, and counters read-only."""
    command = (
        "$ErrorActionPreference='SilentlyContinue'; "
        "$ips=@(Get-NetIPAddress -AddressFamily IPv4 -AddressState Preferred | "
        "Where-Object {$_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1'} | "
        "Select-Object InterfaceIndex,IPAddress); "
        "$routes=@(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' | "
        "Select-Object InterfaceIndex,NextHop,RouteMetric); "
        "$a=@(Get-NetAdapter | ForEach-Object { $n=$_; "
        "$ip=$ips | Where-Object {$_.InterfaceIndex -eq $n.ifIndex} | Select-Object -First 1; "
        "$gw=$routes | Where-Object {$_.InterfaceIndex -eq $n.ifIndex} | "
        "Sort-Object RouteMetric | Select-Object -First 1; "
        "[pscustomobject]@{Name=$n.Name;InterfaceDescription=$n.InterfaceDescription;"
        "Status=$n.Status;LinkSpeed=$n.LinkSpeed;ifIndex=$n.ifIndex;"
        "HardwareInterface=$n.HardwareInterface;IPv4Address=$(if($ip){$ip.IPAddress}else{''});"
        "IPv4Gateway=$(if($gw){$gw.NextHop}else{''})} }); "
        "$s=@(Get-NetAdapterStatistics | Select-Object Name,ReceivedBytes,SentBytes); "
        "[pscustomobject]@{adapters=$a;stats=$s} | ConvertTo-Json -Depth 4 -Compress"
    )
    raw = run_powershell(command)
    try:
        data = json.loads(raw) if raw else {}
        adapters = data.get("adapters", [])
        stats = data.get("stats", [])
        if isinstance(adapters, dict): adapters = [adapters]
        if isinstance(stats, dict): stats = [stats]
        return adapters, stats
    except (json.JSONDecodeError, TypeError, AttributeError):
        return [], []


def connection_kind(adapter):
    label = " ".join(str(adapter.get(k, "")) for k in ("Name", "InterfaceDescription")).lower()
    if adapter.get("HardwareInterface") is False:
        return None
    if any(word in label for word in ("warp", "cloudflare", "tap-windows", "tun", "vpn")):
        return None
    if any(word in label for word in ("wi-fi", "wifi", "wireless", "802.11", "wlan")):
        return "Wi-Fi"
    if any(word in label for word in ("ethernet", "gigabit", "fast ethernet", "gbe", "lan")):
        return "Ethernet"
    return None


def set_login_start(enabled):
    """Set a per-user sign-in entry; this does not require administrator access."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                if getattr(sys, "frozen", False):
                    command = f'"{sys.executable}" --tray'
                else:
                    startup_python = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
                    if not os.path.isfile(startup_python):
                        startup_python = sys.executable
                    command = f'"{startup_python}" "{os.path.abspath(__file__)}" --tray'
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except (OSError, ImportError):
        return False


class StatCard(QFrame):
    def __init__(self, eyebrow, value, detail):
        super().__init__(); self.setObjectName("card")
        layout = QVBoxLayout(self); layout.setContentsMargins(18, 16, 18, 16)
        title = QLabel(eyebrow.upper()); title.setObjectName("eyebrow")
        self.value = QLabel(value); self.value.setObjectName("metric")
        detail_label = QLabel(detail); detail_label.setObjectName("muted")
        layout.addWidget(title); layout.addWidget(self.value); layout.addWidget(detail_label)


class AdapterCard(QFrame):
    def __init__(self, name, subtitle, status, speed):
        super().__init__(); self.setObjectName("adapterCard")
        self.setMinimumHeight(60)
        left = QVBoxLayout(); right = QVBoxLayout()
        name_label = QLabel(name); name_label.setObjectName("adapterName")
        if len(subtitle) > 68:
            subtitle = subtitle[:65] + "…"
        subtitle_label = QLabel(subtitle); subtitle_label.setObjectName("muted")
        status_label = QLabel(status); status_label.setObjectName("status")
        speed_label = QLabel(speed); speed_label.setObjectName("muted")
        left.addWidget(name_label); left.addWidget(subtitle_label)
        right.addWidget(status_label, alignment=Qt.AlignmentFlag.AlignRight)
        right.addWidget(speed_label, alignment=Qt.AlignmentFlag.AlignRight)
        row = QHBoxLayout(self); row.setContentsMargins(16, 12, 16, 12)
        row.addLayout(left); row.addStretch(); row.addLayout(right)


class MainWindow(QMainWindow):
    def __init__(self, start_hidden=False):
        super().__init__()
        self.settings = QSettings("NetFusion", "NetFusion")
        self.proxy_lease = SystemProxyLease(self.settings)
        self.proxy_engine = None
        self.system_proxy_managed = False
        self.boost_active = False
        self._eligible = False
        self._boost_paths = []
        self._active_path_signature = ()
        self._last_counters = {}
        self._last_sample = time.monotonic()
        self._tray_exit = False
        self._snapshot_thread = None
        self._refresh_pending = False
        self._online_kinds = set()
        self._ready_kinds = set()
        self._warp_active = False
        self.setWindowIcon(app_icon())
        self.setWindowTitle("NetFusion"); self.resize(920, 700); self.setMinimumSize(720, 560)
        self.build_ui(); self.setup_tray(); self.refresh()
        self.timer = QTimer(self); self.timer.timeout.connect(self.refresh); self.timer.start(8000)
        if start_hidden and QSystemTrayIcon.isSystemTrayAvailable():
            self.hide()

    def build_ui(self):
        root = QWidget(); root.setObjectName("root"); self.setCentralWidget(root)
        outer = QVBoxLayout(root); outer.setContentsMargins(26, 20, 26, 18); outer.setSpacing(14)

        header = QHBoxLayout()
        titlebox = QVBoxLayout(); titlebox.setSpacing(3)
        title = QLabel("NetFusion"); title.setObjectName("pageTitle")
        desc = QLabel(f"Version {APP_VERSION} · Your connections stay untouched until Boost."); desc.setObjectName("muted")
        titlebox.addWidget(title); titlebox.addWidget(desc); header.addLayout(titlebox); header.addStretch()
        self.engine_badge = QLabel("PASSIVE"); self.engine_badge.setObjectName("statusBadge")
        header.addWidget(self.engine_badge, alignment=Qt.AlignmentFlag.AlignVCenter); outer.addLayout(header)

        scroll = QScrollArea(); scroll.setObjectName("contentScroll"); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget(); cv = QVBoxLayout(content); cv.setContentsMargins(0, 2, 8, 6); cv.setSpacing(12)
        scroll.setWidget(content); outer.addWidget(scroll, 1)

        hero = QFrame(); hero.setObjectName("hero"); hv = QVBoxLayout(hero)
        hv.setContentsMargins(18, 16, 18, 16); hv.setSpacing(8)
        heading = QLabel("Connections"); heading.setObjectName("sectionTitle"); hv.addWidget(heading)
        self.connection_summary = QLabel("Checking your connections…"); self.connection_summary.setObjectName("heroTitle")
        hv.addWidget(self.connection_summary)
        self.engine_text = QLabel(); self.engine_text.setObjectName("muted"); self.engine_text.setWordWrap(True)
        hv.addWidget(self.engine_text)
        actions = QHBoxLayout(); actions.setSpacing(8)
        self.start_btn = QPushButton("Boost Connections"); self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self.start_boost)
        self.stop_btn = QPushButton("Stop Boost"); self.stop_btn.clicked.connect(self.stop_boost)
        self.stop_btn.setEnabled(False)
        refresh_btn = QPushButton("Refresh"); refresh_btn.clicked.connect(self.refresh)
        actions.addWidget(self.start_btn); actions.addWidget(self.stop_btn); actions.addStretch(); actions.addWidget(refresh_btn)
        hv.addLayout(actions)
        self.boost_hint = QLabel(); self.boost_hint.setObjectName("smallMuted"); self.boost_hint.setWordWrap(True)
        hv.addWidget(self.boost_hint); cv.addWidget(hero)

        metrics = QHBoxLayout(); metrics.setSpacing(10)
        self.adapters_card = StatCard("Connections ready", "0 / 2", "Ethernet + Wi-Fi")
        self.download_card = StatCard("Download", "—", "MB/s")
        self.upload_card = StatCard("Upload", "—", "MB/s")
        for card in (self.adapters_card, self.download_card, self.upload_card):
            metrics.addWidget(card)
        cv.addLayout(metrics)

        section = QHBoxLayout(); section.setContentsMargins(2, 4, 2, 0)
        section_title = QLabel("Available connections"); section_title.setObjectName("sectionTitle")
        section.addWidget(section_title); section.addStretch()
        self.adapters_hint = QLabel("Updates every 8 seconds"); self.adapters_hint.setObjectName("smallMuted")
        section.addWidget(self.adapters_hint); cv.addLayout(section)
        self.adapter_layout = QVBoxLayout(); self.adapter_layout.setSpacing(6); cv.addLayout(self.adapter_layout)

        settings_card = QFrame(); settings_card.setObjectName("subtleCard")
        settings_layout = QVBoxLayout(settings_card); settings_layout.setContentsMargins(14, 10, 14, 10)
        self.login_checkbox = QCheckBox("Launch at Windows sign-in and stay in the tray")
        self.login_checkbox.setChecked(bool(
            self.settings.value("launchAtLogin", True, type=bool)
        ))
        self.login_checkbox.toggled.connect(self.set_login_preference); settings_layout.addWidget(self.login_checkbox)
        self.warp_label = QLabel("Cloudflare WARP is not configured by NetFusion while passive.")
        self.warp_label.setObjectName("smallMuted"); self.warp_label.setWordWrap(True)
        settings_layout.addWidget(self.warp_label); cv.addWidget(settings_card)
        note = QLabel(
            "Boost keeps each website on one link and distributes different websites across links. "
            "One connection is not split across links."
        )
        note.setObjectName("smallMuted"); note.setWordWrap(True); cv.addWidget(note)

    def setup_tray(self):
        self.tray = QSystemTrayIcon(self); self.tray.setToolTip("NetFusion — passive network monitor")
        self.tray.setIcon(app_icon())
        menu = QMenu()
        show = QAction("Open NetFusion", self); show.triggered.connect(self.show_window); menu.addAction(show)
        self.tray_boost_action = QAction("Boost Connections", self)
        self.tray_boost_action.triggered.connect(self.start_boost); menu.addAction(self.tray_boost_action)
        self.tray_stop_action = QAction("Stop boost", self)
        self.tray_stop_action.triggered.connect(self.stop_boost); self.tray_stop_action.setEnabled(False)
        menu.addAction(self.tray_stop_action)
        refresh = QAction("Refresh adapters", self); refresh.triggered.connect(self.refresh); menu.addAction(refresh)
        menu.addSeparator()
        quit_action = QAction("Exit NetFusion", self); quit_action.triggered.connect(self.exit_app); menu.addAction(quit_action)
        self.tray.setContextMenu(menu); self.tray.activated.connect(self.tray_activated)
        if QSystemTrayIcon.isSystemTrayAvailable(): self.tray.show()

    def show_window(self):
        self.showNormal(); self.raise_(); self.activateWindow()
        self.timer.setInterval(8000)
        self.refresh()

    def tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick: self.show_window()

    def set_login_preference(self, enabled):
        if not set_login_start(enabled):
            QMessageBox.warning(self, "Startup setting", "Could not update the Windows sign-in startup entry.")
            self.login_checkbox.blockSignals(True); self.login_checkbox.setChecked(not enabled)
            self.login_checkbox.blockSignals(False); return
        self.settings.setValue("launchAtLogin", enabled)

    def start_boost(self):
        self.refresh()
        if self._warp_active:
            QMessageBox.information(
                self,
                "Cloudflare WARP is active",
                "Boost is paused while a WARP adapter is active. The local proxy pins new connections "
                "to the selected physical adapter, and WARP coexistence has not been validated yet. "
                "NetFusion has not changed your network settings.",
            )
            return
        if not self._eligible:
            missing = [name for name in ("Ethernet", "Wi-Fi") if name not in self._ready_kinds]
            missing_text = " and ".join(missing)
            QMessageBox.information(
                self, f"{missing_text} connection needed",
                f"NetFusion needs an active IPv4 address and gateway on both Ethernet and Wi-Fi. "
                f"It is currently missing: {missing_text}. Connect both links, refresh, then try again.",
            )
            return
        if self.boost_active:
            return
        engine = LocalProxyEngine(self._boost_paths)
        proxy_started = False
        try:
            engine.start()
            proxy_started = True
            takeover = self.proxy_lease.acquire()
        except Exception as exc:
            recovery_ok, recovery_message = recover_pending_system_proxy(self.settings)
            recovery_failed = bool(recovery_message and not recovery_ok)
            if proxy_started and recovery_failed:
                # Keep the proxy alive if rollback failed; otherwise apps may
                # still point at localhost after its listener has disappeared.
                self.proxy_engine = engine
                self.system_proxy_managed = True
                self.boost_active = True
                self._active_path_signature = self._path_signature(self._boost_paths)
                self.update_mode_ui()
            else:
                engine.stop()
            recovery_detail = (
                "\n\nProxy recovery status: " + recovery_message
                if recovery_message else ""
            )
            QMessageBox.warning(
                self, "Could not start Boost",
                f"NetFusion could not start its local proxy or prepare the Windows proxy setting.\n\n{exc}"
                f"{recovery_detail}",
            )
            return
        self.proxy_engine = engine
        self._active_path_signature = self._path_signature(self._boost_paths)
        self.system_proxy_managed = takeover == "managed"
        self.boost_active = True
        self.update_mode_ui()
        message = (
            f"Boost is active. New proxy connections are assigned across available links."
            if self.system_proxy_managed else
            f"Local proxy is active at {HTTP_PROXY_HOST}:{HTTP_PROXY_PORT}; existing Windows proxy settings "
            "were preserved. Configure an app to use this HTTP proxy or SOCKS5 at "
            f"{HTTP_PROXY_HOST}:{SOCKS_PROXY_PORT}."
        )
        self.tray.showMessage("NetFusion", message, QSystemTrayIcon.MessageIcon.Information, 5000)

    def stop_boost(self, reason=None):
        if not self.boost_active:
            return True
        was_managed = self.system_proxy_managed
        if was_managed:
            restored, restore_message = self.proxy_lease.release()
            if not restored:
                self.engine_badge.setText("●  RECOVERY NEEDS ATTENTION")
                self.engine_text.setText(
                    "NetFusion could not restore the Windows proxy setting. The local proxy is still running "
                    "so traffic is not sent to a closed local port. Resolve the registry error, then try Stop again."
                )
                self.tray.showMessage(
                    "NetFusion could not stop safely", restore_message,
                    QSystemTrayIcon.MessageIcon.Critical, 10000,
                )
                return False
        else:
            restored, restore_message = True, ""
        engine = self.proxy_engine
        self.proxy_engine = None
        if engine is not None:
            engine.stop()
        self.system_proxy_managed = False
        self.boost_active = False
        self._active_path_signature = ()
        self.update_mode_ui()
        if self.timer:
            self.timer.setInterval(30000 if self.isHidden() else 8000)
        if restore_message and not restored:
            message = restore_message
        else:
            default_message = (
                "Boost stopped. NetFusion restored the previous proxy settings; routes and DNS were not changed."
                if was_managed else
                "Boost stopped. Windows proxy settings were left unchanged; routes and DNS were not changed."
            )
            message = reason or default_message
            if restore_message and "changed by another app" in restore_message:
                message = restore_message
        self.tray.showMessage("NetFusion", message, QSystemTrayIcon.MessageIcon.Information, 5000)
        return True

    def update_mode_ui(self):
        if self.boost_active:
            self.engine_badge.setText("●  BOOST ACTIVE")
            if self.system_proxy_managed:
                self.engine_text.setText(
                    "Local HTTP/HTTPS and SOCKS5 proxies are active. Each destination host stays on one link; "
                    "different hosts are distributed across Ethernet and Wi-Fi."
                )
            else:
                self.engine_text.setText(
                    f"Local proxies are active at {HTTP_PROXY_HOST}:{HTTP_PROXY_PORT} (HTTP/HTTPS) and "
                    f"{HTTP_PROXY_HOST}:{SOCKS_PROXY_PORT} (SOCKS5). Windows proxy settings were left unchanged."
                )
        else:
            self.engine_badge.setText("●  PASSIVE MONITORING")
            self.engine_text.setText(
                "Passive mode only reads adapter status and counters. The local proxy is stopped; routes, "
                "DNS, Windows proxy settings, and Cloudflare WARP are untouched."
            )
        if self.boost_active:
            self.start_btn.setText("Boost Active")
            self.boost_hint.setText(
                "Connections to the same host stay together. One TCP connection is not split across links."
            )
        else:
            self.start_btn.setText("Boost Connections")
            missing = [name for name in ("Ethernet", "Wi-Fi") if name not in self._ready_kinds]
            self.boost_hint.setText(
                "Both links have an IPv4 address and gateway."
                if not missing else "Waiting for a working IPv4 path on: " + " + ".join(missing) + "."
            )
        # Keep this clickable while links are missing so the user gets a clear
        # explanation instead of an apparently dead button.
        self.start_btn.setEnabled(not self.boost_active)
        self.stop_btn.setEnabled(self.boost_active)
        self.tray_boost_action.setEnabled(not self.boost_active)
        self.tray_stop_action.setEnabled(self.boost_active)
        if hasattr(self, "timer"):
            self.timer.setInterval(
                5000 if self.boost_active and self.isHidden()
                else 30000 if self.isHidden()
                else 8000
            )

    def refresh(self):
        if self._snapshot_thread is not None and self._snapshot_thread.isRunning():
            self._refresh_pending = True
            return
        self._refresh_pending = False
        thread = SnapshotThread(self)
        self._snapshot_thread = thread
        thread.snapshot_ready.connect(self.apply_snapshot)
        thread.finished.connect(self.snapshot_finished)
        thread.start()

    def snapshot_finished(self):
        thread = self._snapshot_thread
        self._snapshot_thread = None
        if thread is not None:
            thread.deleteLater()
        if self._refresh_pending:
            self.refresh()

    def apply_snapshot(self, adapters, counter_rows):
        counters = {x.get("Name"): x for x in counter_rows if x.get("Name")}
        physical = []
        for adapter in adapters:
            kind = connection_kind(adapter)
            if kind:
                adapter["_kind"] = kind; physical.append(adapter)
        online = {a["_kind"] for a in physical if a.get("Status") == "Up"}
        self._online_kinds = online
        self._boost_paths = []
        for adapter in physical:
            if adapter.get("Status") != "Up":
                continue
            address = str(adapter.get("IPv4Address", "") or "").strip()
            gateway = str(adapter.get("IPv4Gateway", "") or "").strip()
            try:
                interface_index = int(adapter.get("ifIndex", 0))
            except (TypeError, ValueError):
                interface_index = 0
            if address and gateway and interface_index > 0:
                self._boost_paths.append(AdapterPath(
                    adapter.get("_kind", "Network") + " · " + adapter.get("Name", "Adapter"),
                    interface_index,
                    address,
                    gateway,
                ))
        self._ready_kinds = {path.name.split(" · ", 1)[0] for path in self._boost_paths}
        self._eligible = {"Ethernet", "Wi-Fi"}.issubset(self._ready_kinds)
        self.adapters_card.value.setText(f"{len(self._ready_kinds)} / 2")
        if self._eligible:
            self.connection_summary.setText("Both connections are available")
        elif "Ethernet" in online:
            self.connection_summary.setText("Ethernet is connected")
        elif "Wi-Fi" in online:
            self.connection_summary.setText("Wi-Fi is connected")
        else:
            self.connection_summary.setText("Waiting for a connection")
        warp_found = any(
            any(word in " ".join(str(a.get(k, "")) for k in ("Name", "InterfaceDescription")).lower()
                for word in ("warp", "cloudflare")) for a in adapters
        )
        self._warp_active = any(
            a.get("Status") == "Up" and
            any(word in " ".join(str(a.get(k, "")) for k in ("Name", "InterfaceDescription")).lower()
                for word in ("warp", "cloudflare"))
            for a in adapters
        )
        self.warp_label.setText(
            (
                "WARP adapter is active. Boost is disabled until active-mode compatibility is validated; "
                "passive monitoring leaves WARP untouched."
                if self._warp_active else
                "Cloudflare WARP adapter detected. It is left untouched while NetFusion is passive."
            ) if warp_found
            else "Cloudflare WARP is left untouched while NetFusion is passive."
        )

        now = time.monotonic(); elapsed = max(now - self._last_sample, 0.01)
        current = {}; rx_delta = tx_delta = 0
        for adapter in physical:
            name = adapter.get("Name", ""); count = counters.get(name, {})
            rx = int(count.get("ReceivedBytes", 0) or 0); tx = int(count.get("SentBytes", 0) or 0)
            current[name] = (rx, tx)
            if name in self._last_counters:
                old_rx, old_tx = self._last_counters[name]
                rx_delta += max(0, rx - old_rx); tx_delta += max(0, tx - old_tx)
        if self._last_counters:
            self.download_card.value.setText(f"{rx_delta / elapsed / 1_000_000:.2f} MB/s")
            self.upload_card.value.setText(f"{tx_delta / elapsed / 1_000_000:.2f} MB/s")
        self._last_counters = current; self._last_sample = now

        while self.adapter_layout.count():
            item = self.adapter_layout.takeAt(0)
            if item is not None:
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()
        for adapter in physical:
            self.adapter_layout.addWidget(AdapterCard(
                adapter.get("_kind", "Network") + " · " + adapter.get("Name", "Adapter"),
                adapter.get("InterfaceDescription", ""),
                "●  " + str(adapter.get("Status", "Unknown")), str(adapter.get("LinkSpeed", "")),
            ))
        if not physical:
            label = QLabel("No physical Ethernet or Wi-Fi adapters found."); label.setObjectName("muted")
            self.adapter_layout.addWidget(label)
        if self.boost_active and self._warp_active:
            self.stop_boost(
                "Cloudflare WARP became active. Boost stopped and NetFusion restored proxy settings."
            )
        elif self.boost_active and not self._eligible:
            self.stop_boost(
                "A selected link lost its IPv4 path. Boost stopped and NetFusion restored proxy settings."
            )
        elif self.boost_active and self._path_signature(self._boost_paths) != self._active_path_signature:
            self.stop_boost(
                "A network adapter changed while Boost was active. Boost stopped and NetFusion restored proxy settings."
            )
        else:
            self.update_mode_ui()

    @staticmethod
    def _path_signature(paths):
        return tuple(sorted(
            (path.name, path.interface_index, path.ipv4_address, path.gateway)
            for path in paths
        ))

    def exit_app(self):
        if not self.stop_boost(
            "NetFusion exited. Boost stopped and proxy settings were restored."
        ):
            self._tray_exit = False
            self.showNormal()
            QMessageBox.critical(
                self,
                "Cannot exit safely",
                "NetFusion kept Boost running because Windows proxy recovery failed. Fix the reported "
                "setting problem and use Stop Boost before exiting, so apps are not left pointing at a closed proxy.",
            )
            return
        self._tray_exit = True
        if self._snapshot_thread is not None and self._snapshot_thread.isRunning():
            self._snapshot_thread.wait(7000)
        self.tray.hide(); QApplication.quit()

    def closeEvent(self, event):
        if not self._tray_exit:
            if not self.stop_boost(
                "The dashboard closed, so Boost stopped and proxy settings were restored. NetFusion remains in the tray."
            ):
                event.ignore()
                self.showNormal()
                return
            if QSystemTrayIcon.isSystemTrayAvailable():
                event.ignore(); self.hide()
                self.tray.showMessage(
                    "NetFusion", "Still running in the system tray.",
                    QSystemTrayIcon.MessageIcon.Information, 2500,
                )
            else:
                event.accept()
        else:
            event.accept()

    def hideEvent(self, event):
        self.timer.setInterval(5000 if self.boost_active else 30000)
        super().hideEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setOrganizationName("NetFusion")
    app.setApplicationName(APP_NAME)
    app.setFont(QFont("Segoe UI", 10))
    lock_dir = Path(QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppLocalDataLocation
    ))
    lock_dir.mkdir(parents=True, exist_ok=True)
    instance_lock = QLockFile(str(lock_dir / "NetFusion.lock"))
    instance_lock.setStaleLockTime(30000)
    if not instance_lock.tryLock(0):
        if "--tray" not in sys.argv:
            QMessageBox.information(None, "NetFusion is already running", "Open NetFusion from the system tray.")
        sys.exit(0)
    app.setWindowIcon(app_icon())
    app.setStyleSheet("""
        QWidget#root { background:#0b1020; color:#edf2ff; }
        QScrollArea#contentScroll, QScrollArea#contentScroll QWidget#qt_scrollarea_viewport { background:transparent; border:0; }
        QLabel#pageTitle { font-size:24px; font-weight:700; color:#f3f6ff; }
        QLabel#sectionTitle { font-size:15px; font-weight:650; color:#e8eefb; }
        QLabel#heroTitle { font-size:20px; font-weight:650; color:#f3f6ff; }
        QLabel#muted { color:#a6b2c8; font-size:12px; }
        QLabel#smallMuted { color:#8998b2; font-size:11px; }
        QLabel#statusBadge { color:#b9f4d8; background:#142a28; padding:7px 12px; border-radius:10px; font-size:11px; font-weight:700; }
        QFrame#hero, QFrame#card, QFrame#adapterCard, QFrame#subtleCard { background:#111a2b; border:1px solid #263550; border-radius:12px; }
        QFrame#hero { background:#121e31; }
        QFrame#adapterCard { background:#10192a; }
        QLabel#eyebrow { color:#8998b2; font-size:10px; font-weight:700; letter-spacing:1px; }
        QLabel#metric { font-size:21px; font-weight:650; color:#8ee7ff; }
        QLabel#adapterName { font-size:13px; font-weight:650; color:#e8eefb; }
        QLabel#status { color:#70e1b2; font-weight:650; font-size:11px; }
        QPushButton { color:#e8eefb; background:#1b2940; border:1px solid #334563; border-radius:8px; padding:8px 12px; font-size:12px; }
        QPushButton:hover { background:#263a59; } QPushButton:disabled { color:#748097; background:#141c2b; }
        QPushButton#primary { color:#07111e; background:#8ee7ff; border:0; font-weight:700; }
        QPushButton#primary:hover { background:#b1efff; }
        QPushButton#primary:disabled { color:#75839a; background:#202b3d; border:1px solid #303d52; }
        QCheckBox { color:#c5d0e4; spacing:8px; font-size:12px; }
        QScrollBar:vertical { background:transparent; width:8px; margin:2px; }
        QScrollBar::handle:vertical { background:#334563; border-radius:4px; min-height:24px; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
    """)
    settings = QSettings("NetFusion", "NetFusion")
    recovery_ok, recovery_message = recover_pending_system_proxy(settings)
    if recovery_message and not recovery_ok:
        QMessageBox.critical(
            None,
            "Windows proxy recovery failed",
            recovery_message + "\n\nNetFusion will not start until the interrupted Boost proxy settings can be recovered.",
        )
        sys.exit(1)
    launch_at_login = settings.value("launchAtLogin", True, type=bool)
    settings.setValue("launchAtLogin", launch_at_login)
    if launch_at_login: set_login_start(True)
    tray_mode = "--tray" in sys.argv
    window = MainWindow(start_hidden=tray_mode)
    if recovery_message:
        QTimer.singleShot(
            0,
            lambda message=recovery_message: window.tray.showMessage(
                "NetFusion recovery", message, QSystemTrayIcon.MessageIcon.Warning, 7000
            ),
        )
    if not (tray_mode and QSystemTrayIcon.isSystemTrayAvailable()): window.show()
    sys.exit(app.exec())

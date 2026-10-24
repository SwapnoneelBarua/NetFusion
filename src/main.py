"""NetFusion monitor. Boost preview is deliberately monitor-only."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QSettings, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QFont, QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPushButton, QSystemTrayIcon, QMenu, QVBoxLayout, QWidget,
    QScrollArea,
)

APP_NAME = "NetFusion"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


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
    """Fetch adapter state and counters in one read-only PowerShell process."""
    command = (
        "$ErrorActionPreference='SilentlyContinue'; "
        "$a=@(Get-NetAdapter | Select-Object Name,InterfaceDescription,Status,LinkSpeed,"
        "ifIndex,HardwareInterface); "
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
        self.preview_active = False
        self._eligible = False
        self._last_counters = {}
        self._last_sample = time.monotonic()
        self._tray_exit = False
        self._snapshot_thread = None
        self._refresh_pending = False
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
        desc = QLabel("Your connections stay untouched until you ask for Boost."); desc.setObjectName("muted")
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
        self.start_btn = QPushButton("Boost Connections (preview)"); self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self.start_preview)
        self.stop_btn = QPushButton("Stop Boost"); self.stop_btn.clicked.connect(self.stop_preview)
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
        note = QLabel("This build monitors readiness only. Actual connection bonding is not implemented yet.")
        note.setObjectName("smallMuted"); note.setWordWrap(True); cv.addWidget(note)

    def setup_tray(self):
        self.tray = QSystemTrayIcon(self); self.tray.setToolTip("NetFusion — passive network monitor")
        self.tray.setIcon(app_icon())
        menu = QMenu()
        show = QAction("Open NetFusion", self); show.triggered.connect(self.show_window); menu.addAction(show)
        self.tray_boost_action = QAction("Boost Connections (preview)", self)
        self.tray_boost_action.triggered.connect(self.start_preview); menu.addAction(self.tray_boost_action)
        self.tray_stop_action = QAction("Stop boost", self)
        self.tray_stop_action.triggered.connect(self.stop_preview); self.tray_stop_action.setEnabled(False)
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

    def start_preview(self):
        self.refresh()
        if not self._eligible:
            missing = [name for name in ("Ethernet", "Wi-Fi") if name not in self._online_kinds]
            missing_text = " and ".join(missing)
            QMessageBox.information(
                self, f"{missing_text} connection needed",
                f"NetFusion currently sees {missing_text} as disconnected. Connect both physical Ethernet "
                "and Wi-Fi, then click Boost Connections again. NetFusion remains passive for now.",
            )
            return
        self.preview_active = True; self.update_mode_ui()
        self.tray.showMessage("NetFusion", "Boost preview active. Windows normal routing is unchanged.",
                              QSystemTrayIcon.MessageIcon.Information, 3000)

    def stop_preview(self, reason=None):
        if not self.preview_active: return
        self.preview_active = False; self.update_mode_ui()
        self.tray.showMessage("NetFusion", reason or "Preview stopped. Network settings were not changed.",
                              QSystemTrayIcon.MessageIcon.Information, 3500)

    def update_mode_ui(self):
        if self.preview_active:
            self.engine_badge.setText("●  BOOST PREVIEW · MONITOR ONLY")
            self.engine_text.setText(
                "Preview is active because you requested it. NetFusion checks both links, but this build has "
                "no traffic-balancing engine; Windows routing and WARP remain untouched."
            )
        else:
            self.engine_badge.setText("●  PASSIVE MONITORING")
            self.engine_text.setText(
                "Passive mode only reads adapter status and counters. It does not change routes, DNS, "
                "proxy settings, or Cloudflare WARP."
            )
        if self.preview_active:
            self.start_btn.setText("Boost Preview Active")
            self.boost_hint.setText("Monitor-only preview. Internet traffic is not being combined.")
        else:
            self.start_btn.setText("Boost Connections (preview)")
            missing = [name for name in ("Ethernet", "Wi-Fi") if name not in self._online_kinds]
            self.boost_hint.setText(
                "Both links are ready. Preview only; this build does not combine traffic."
                if not missing else "Click Boost for details. Waiting for: " + " + ".join(missing) + "."
            )
        # Keep this clickable while links are missing so the user gets a clear
        # explanation instead of an apparently dead button.
        self.start_btn.setEnabled(not self.preview_active)
        self.stop_btn.setEnabled(self.preview_active)
        self.tray_boost_action.setEnabled(not self.preview_active)
        self.tray_stop_action.setEnabled(self.preview_active)

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
        self._eligible = {"Ethernet", "Wi-Fi"}.issubset(online)
        self.adapters_card.value.setText(f"{len(online)} / 2")
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
        self.warp_label.setText(
            "Cloudflare WARP adapter detected. It is left untouched while NetFusion is passive." if warp_found
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
        if self.preview_active and not self._eligible:
            self.stop_preview("Ethernet or Wi-Fi disconnected. Preview stopped; the remaining link is unchanged.")
        else:
            self.update_mode_ui()

    def exit_app(self):
        self._tray_exit = True
        self.stop_preview("NetFusion exited. Windows network settings were not changed.")
        if self._snapshot_thread is not None and self._snapshot_thread.isRunning():
            self._snapshot_thread.wait(7000)
        self.tray.hide(); QApplication.quit()

    def closeEvent(self, event):
        if not self._tray_exit and QSystemTrayIcon.isSystemTrayAvailable():
            event.ignore(); self.hide()
            self.tray.showMessage(
                "NetFusion", "Still running in the system tray.",
                QSystemTrayIcon.MessageIcon.Information, 2500,
            )
        else: event.accept()

    def hideEvent(self, event):
        self.timer.setInterval(30000)
        super().hideEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv); app.setApplicationName(APP_NAME); app.setFont(QFont("Segoe UI", 10))
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
    launch_at_login = settings.value("launchAtLogin", True, type=bool)
    settings.setValue("launchAtLogin", launch_at_login)
    if launch_at_login: set_login_start(True)
    tray_mode = "--tray" in sys.argv
    window = MainWindow(start_hidden=tray_mode)
    if not (tray_mode and QSystemTrayIcon.isSystemTrayAvailable()): window.show()
    sys.exit(app.exec())

# NetFusion (safe monitoring prototype)

NetFusion is a Windows 10/11 desktop monitor for physical Ethernet and Wi-Fi adapters. It starts in passive mode. Passive mode reads adapter status and byte counters; it does not change Windows routes, DNS, proxy settings, firewall rules, or Cloudflare WARP.

## Current behavior

- The app can add itself to the current Windows user's sign-in startup entries. The setting is enabled by default and can be turned off in the dashboard. Startup launches the app hidden in the system tray.
- Adapter state and counters are fetched together through one read-only PowerShell call. The dashboard refreshes every eight seconds while open and every 30 seconds while hidden in the tray to reduce background work. Only physical Ethernet and Wi-Fi are included in readiness and throughput; virtual adapters such as WARP are only checked for display and are never changed.
- The Boost button stays clickable and explains which link is missing. A monitor-only preview starts only when a physical Ethernet and Wi-Fi adapter are both up. If either link goes down, the preview stops and the app returns to passive mode.
- Boost preview is a readiness/monitoring state. It does not combine, route, or balance traffic. No working bonding engine exists in this build.
- A single download split across both ISPs generally needs a remote aggregation server as well as a routing/tunnel engine. The UI does not claim otherwise.

## Run on Windows

From the project folder in PowerShell:

```powershell
py -3.14 -m venv .venv314
.\.venv314\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python .\src\main.py
```

The first run enables launch at sign-in by default and stores a per-user Run entry. In the dashboard, clear **Launch at Windows sign-in and stay in the tray** to remove that entry. To exit instead of minimizing to tray, use the tray icon's **Exit NetFusion** menu item.

To run it directly in the tray for a manual startup check:

```powershell
python .\src\main.py --tray
```

## Manual behavior check

1. Start the app with Ethernet only. Confirm it says **Passive monitoring** and Windows networking behaves normally.
2. Connect Wi-Fi. Confirm both adapters show as up and the preview button becomes available.
3. Start **Boost preview**. Confirm the status says **Monitor only** and that the app states Windows routing is unchanged.
4. Disconnect Wi-Fi. Within the next refresh, confirm the preview stops and Ethernet remains connected.
5. Start and stop the preview from the tray menu. Confirm the app returns to passive monitoring.
6. Toggle the sign-in checkbox and verify the per-user startup entry through Windows Task Manager's Startup Apps view.

This is a prototype; these are manual checks, not an automated test suite. Real traffic bonding and Cloudflare WARP coexistence under an active routing engine remain unimplemented.

## Build a Windows x64 installer

Build on 64-bit Windows with Python 3.14 and Inno Setup 6 installed. The build script creates a separate `.venv-build314` environment, uses PySide6 Essentials 6.9.3 (QtCore, QtGui, and QtWidgets), and rebuilds the app with PyInstaller before making the installer. It also isolates packaging from unrelated native DLLs on `PATH`, which previously caused QtCore to bind to an incompatible ICU library. Then run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\build_windows.ps1
```

The build pins PySide6 Essentials to 6.9.3 and isolates PyInstaller from unrelated Conda and toolchain DLLs on `PATH`; newer Qt wheels in this environment can resolve an incompatible ICU DLL and fail during startup. The installer is written to `dist\installer\NetFusion-Setup-x64.exe`. It installs per-user under Local AppData, adds a Start Menu shortcut, and can optionally add a desktop shortcut. It includes the app runtime, so end users do not need Python.

The frozen app registers its bundled PySide6 and Shiboken DLL directories before importing Qt. This avoids relying on a developer machine's PATH when launching the installed app. The installer is currently unsigned, so Windows SmartScreen may show an unknown-publisher warning. Public release should use a trusted code-signing certificate. The Inno Setup 6 compiler used for this build states non-commercial use only; obtain the appropriate license before commercial distribution.

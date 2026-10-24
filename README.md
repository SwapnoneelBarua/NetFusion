# NetFusion (safe monitoring prototype)

NetFusion is a Windows 10/11 desktop monitor for physical Ethernet and Wi-Fi adapters. It starts in passive mode. Passive mode reads adapter status and byte counters; it does not change Windows routes, DNS, proxy settings, firewall rules, or Cloudflare WARP.

## Current behavior

- The app can add itself to the current Windows user's sign-in startup entries. The setting is enabled by default and can be turned off in the dashboard. Startup launches the app hidden in the system tray.
- Adapter state, IPv4 addresses, default gateways, and counters are fetched in one read-only PowerShell call on a background thread. The dashboard refreshes every eight seconds while open and every 30 seconds while hidden in the tray. Readiness requires an active physical Ethernet and Wi-Fi path with an IPv4 address and gateway.
- Passive mode never starts the proxy or changes Windows proxy, routes, DNS, or Cloudflare WARP. Startup remains passive.
- Boost starts local HTTP/HTTPS (`127.0.0.1:10801`) and SOCKS5 (`127.0.0.1:10800`) proxies. The scheduler assigns each new connection to one selected adapter using a Windows per-socket interface setting. It does not change the routing table or DNS.
- If no Windows proxy, PAC URL, or auto-detect proxy is active, Boost temporarily points the current user's Windows proxy setting at NetFusion for apps that honor the Windows user proxy. It first stores an encrypted recovery record and restores the previous values on Stop, adapter loss, or normal app exit. If an existing proxy configuration is active, NetFusion leaves it alone and shows the local addresses for manual app setup.
- The local proxy distributes separate IPv4 TCP connections. It does not split one TCP connection across links, and applications that ignore the Windows proxy need to be configured manually. IPv6 destinations are not supported in this first proxy build. This follows the same multi-connection boundary described in the [HypoMux aggregation guide](https://hypomux.com/en/docs/aggregation).
- Boost is disabled while a Cloudflare WARP adapter is active, because active-mode coexistence has not yet been validated. Passive monitoring leaves WARP untouched.

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

1. Start NetFusion with Ethernet only. Confirm it says **Passive monitoring** and the Windows proxy settings are unchanged.
2. Connect Wi-Fi and confirm both adapters have an IPv4 address and default gateway.
3. Start **Boost Connections**. If Windows has no active proxy/PAC configuration, verify the Windows user proxy points to `127.0.0.1:10801`; otherwise configure a test app manually. The SOCKS5 endpoint is `127.0.0.1:10800`.
4. Use a multi-connection download and inspect whether new connections use both links. One connection remains on one link.
5. Stop Boost and verify the previous proxy settings return. Disconnect either adapter during Boost and verify it stops and restores the proxy within one refresh interval.
6. With WARP active, confirm Boost is blocked. Toggle the sign-in checkbox and verify the Windows Startup Apps entry.

Local HTTP, HTTPS CONNECT, and SOCKS5 loopback checks pass. Mocked Windows proxy takeover, restoration, interrupted-session recovery, and existing-proxy preservation checks pass. The x64 installer compiles from this source revision. Real traffic across two independent uplinks, Steam compatibility, Windows proxy behavior across different applications, and active WARP coexistence still need end-to-end testing before a stable release.

## Build a Windows x64 installer

Build on 64-bit Windows with Python 3.14 and Inno Setup 6 installed. The build script creates a separate `.venv-build314` environment, uses PySide6 Essentials 6.9.3 (QtCore, QtGui, and QtWidgets), and rebuilds the app with PyInstaller before making the installer. It also isolates packaging from unrelated native DLLs on `PATH`, which previously caused QtCore to bind to an incompatible ICU library. Then run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\build_windows.ps1
```

The build pins PySide6 Essentials to 6.9.3 and isolates both icon generation and PyInstaller from unrelated Conda and toolchain DLLs on `PATH`; newer Qt wheels in this environment can resolve an incompatible ICU DLL and fail during startup. It generates `icon\netfusion.ico` from the supplied `icon\icon.png` and embeds the icon in the app, tray, installer, and shortcuts. The installer is written to `dist\installer\NetFusion-Setup-x64.exe`. It installs per-user under Local AppData, adds a Start Menu shortcut, and can optionally add a desktop shortcut. It includes the app runtime, so end users do not need Python.

The frozen app registers its bundled PySide6 and Shiboken DLL directories before importing Qt. This avoids relying on a developer machine's PATH when launching the installed app. The installer is currently unsigned, so Windows SmartScreen may show an unknown-publisher warning. Public release should use a trusted code-signing certificate. The Inno Setup 6 compiler used for this build states non-commercial use only; obtain the appropriate license before commercial distribution.

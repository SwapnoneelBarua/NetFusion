# NetFusion 1.0.1

NetFusion is a Windows 10/11 x64 tray app with a passive network monitor and an on-demand local multi-path proxy. It starts passive: startup monitoring does not change Windows routes, DNS, proxy settings, firewall rules, or Cloudflare WARP.

## What this build does

- Detects physical Ethernet and Wi-Fi adapters, including Remote NDIS phone-tether adapters, and shows link state, IPv4 gateway, and byte counters.
- Runs quietly in the system tray and can start at Windows sign-in. It never enables Boost automatically.
- On an explicit **Boost Connections** action, starts local HTTP/HTTPS proxy `127.0.0.1:10801` and SOCKS5 proxy `127.0.0.1:10800`. New IPv4 TCP sessions are pinned to an adapter; sessions for the same destination host stay on one link, while new hosts are distributed across eligible links. This host affinity is intended to reduce website session failures caused by switching public IPs between parallel browser connections.
- Uses the Windows user proxy only if no manual proxy, PAC, or auto-detect setting is already active. A protected recovery record allows NetFusion to restore its prior settings on Stop, adapter loss, normal exit, and next launch after an interrupted session. Existing proxy settings are preserved; in that case, configure a test application to use the local proxy endpoints manually.
- Stops Boost if either eligible uplink disappears or a Cloudflare WARP adapter becomes active. WARP remains untouched in passive mode.

## Important limits

This is per-connection distribution, not a virtual network adapter or a full system-wide router. Each TCP connection uses one uplink for its lifetime; NetFusion does not split a single connection or a single download stream across links. A single-download speed increase requires the downloading app to make multiple proxy-aware connections. Some applications ignore the Windows proxy and need manual proxy configuration. IPv6 proxy destinations are not supported. Active WARP coexistence is intentionally blocked and has not been validated. Steam behavior has not yet been confirmed.

The 1.0.1 source has passed local HTTP, HTTPS CONNECT, SOCKS5, host-affinity, stop/recovery mock tests and live direct/proxy reachability checks over two physical interfaces. Those live checks confirm adapter pinning and proxy reachability; they do not prove that Steam or every Windows app uses both links. The Windows proxy lifecycle tests use a mocked registry and do not change this PC's proxy settings. Test Chrome and other target applications after installing the rebuilt package before relying on it for important downloads.

## Run from source

In PowerShell, from the project folder:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python .\src\main.py
```

To open directly into the tray for a startup check:

```powershell
python .\src\main.py --tray
```

The first run enables launch at sign-in by default. Turn it off in the dashboard. Use the tray menu's **Exit NetFusion** item to exit; closing the dashboard minimizes the app to the tray.

## Build the Windows x64 installer

Install 64-bit Python 3.14 and Inno Setup 6, then run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\build_windows.ps1
```

The script builds the app with the pinned PySide6 Essentials runtime and creates `dist\installer\NetFusion-Setup-x64.exe`. The installer includes Python and Qt runtime files, so users do not need to install Python. The installer is currently unsigned; Windows SmartScreen may show an unknown-publisher warning. Inno Setup 6's compiler states non-commercial use only; obtain an appropriate license before commercial distribution.

## Verification

Run the source tests with:

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
python -m unittest discover -s tests -v
```

Version is stored in `VERSION`. Update it before building a new release.

# NetFusion 1.0.2

NetFusion is a Windows 10/11 x64 tray app with a passive network monitor and an on-demand local multi-path proxy. It starts passive: startup monitoring does not change Windows routes, DNS, proxy settings, firewall rules, or Cloudflare WARP.

## What this build does

- Detects physical Ethernet and Wi-Fi adapters, including Remote NDIS phone-tether adapters, and shows link state, IPv4 gateway, and byte counters.
- Runs quietly in the system tray and can start at Windows sign-in. It never enables Boost automatically.
- On an explicit **Boost Connections** action, starts local HTTP/HTTPS proxy `127.0.0.1:10801` and SOCKS5 proxy `127.0.0.1:10800`. New IPv4 TCP sessions are pinned to an adapter; sessions for the same destination host stay on one link, while new hosts are distributed across eligible links. This host affinity is intended to reduce website session failures caused by switching public IPs between parallel browser connections.
- Uses bounded 1 MiB relay buffers to allow more queued data per active connection. This increases the maximum buffering compared with earlier builds; under the 64-connection cap, relay buffers can account for up to about 128 MiB while heavily loaded, before socket and runtime overhead.
- Uses the Windows user proxy only if no manual proxy, PAC, or auto-detect setting is already active. A protected recovery record allows NetFusion to restore its prior settings on Stop, adapter loss, normal exit, and next launch after an interrupted session. Existing proxy settings are preserved; in that case, configure a test application to use the local proxy endpoints manually.
- Stops Boost if either eligible uplink disappears or a Cloudflare WARP adapter becomes active. WARP remains untouched in passive mode.

## Important limits

This is per-connection distribution, not a virtual network adapter or a full system-wide router. Each TCP connection uses one uplink for its lifetime; NetFusion does not split a single connection or a single download stream across links. A single-download speed increase requires the downloading app to make multiple proxy-aware connections. Some applications ignore the Windows proxy and need manual proxy configuration. IPv6 proxy destinations are not supported. Active WARP coexistence is intentionally blocked and has not been validated. Steam behavior has not yet been confirmed.

NetFusion does not require administrator privileges. The installer is per-user, and the app only changes the current user's Windows proxy while Boost is explicitly active. It does not alter global TCP settings, routes, DNS, firewall rules, or Cloudflare WARP. If Windows TCP Auto-Tuning is disabled, a user can inspect or change that Windows setting separately; NetFusion does not change it automatically.

Automated tests cover the proxy protocols, host affinity, adapter state transitions, and mocked proxy recovery. Passing those tests does not guarantee compatibility or higher speeds in every browser or application. Test Chrome, Steam, WARP, and other target applications on the intended PC before relying on Boost for important downloads.

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

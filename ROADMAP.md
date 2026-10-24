# NetFusion stable release roadmap

The current `v0.1.0` release is a passive network monitor. It does not combine traffic. A stable release that behaves like HypoMux needs the following work before it can be called `1.0.0`.

## 1. Connection distribution engine

- Start with a local HTTP/HTTPS and SOCKS5 proxy mode for proxy-aware download applications. Let users configure a proxy manually; make Windows system-proxy takeover an explicit active-mode option with exact save/restore behavior.
- Bind each new outbound connection to a selected physical adapter and make the scheduler account for link availability and measured capacity. Keep established connections on their original path.
- Treat broader system-wide routing for apps that ignore proxies as a separate virtual-adapter/Windows-filtering-driver milestone, with its own compatibility and recovery work.
- Define behavior for applications that ignore the configured proxy and for long-lived connections that cannot move after they start.
- Keep the product claim precise: separate connections can use separate uplinks; splitting one TCP connection across both links requires a remote bonding endpoint.

## 2. Safe activation and recovery

- Keep startup and passive mode read-only. Only an explicit Boost action may activate the engine.
- Record the exact proxy, route, DNS, and adapter state the engine changes; restore only changes NetFusion owns.
- Restore state on Stop, link loss, application exit, failed startup, and reboot recovery. Never overwrite changes the user or another VPN made after Boost started.
- Detect and fail closed when WARP, another VPN, proxy, or virtual adapter conflicts with the chosen mode; support active WARP coexistence only after it passes recovery and traffic-path tests.

## 3. Compatibility and reliability

- Test with Ethernet plus Wi-Fi, each link failing during traffic, sleep/resume, sign-in startup, and repeated start/stop cycles.
- Test Cloudflare WARP and other VPN/proxy combinations in the supported active mode, and clearly report unsupported combinations.
- Measure CPU and memory in the tray, during adapter polling, and while Boost is active.
- Test the installer and uninstall/upgrade paths on a clean Windows 10/11 x64 machine; sign the public installer and provide a license and support contact before a production release.

## Current milestone

The supplied app icon is being integrated into the UI and Windows package, and adapter polling has been moved off the UI thread. The actual aggregation engine, active-mode network restoration, and compatibility validation remain unimplemented. The downloadable `v0.1.0` release still represents the original monitor build; publish a new version only after its source and installer have been built and verified together.

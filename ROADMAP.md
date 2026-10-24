# NetFusion stable release roadmap

The current `v0.1.0` release is a passive network monitor. It does not combine traffic. A stable release that behaves like HypoMux needs the following work before it can be called `1.0.0`.

## 1. Connection distribution engine

- A first local HTTP/HTTPS and SOCKS5 proxy engine is implemented on `127.0.0.1`; it assigns each new TCP connection across eligible adapters in round-robin order and pins outbound sockets to an interface.
- Keep the scope clear: this distributes separate connections only. A single TCP connection is not split across links.
- Validate adapter selection and throughput with two independent internet uplinks, including Steam and other proxy-aware download applications. Established connections stay on their original path.
- Treat broader system-wide routing for apps that ignore proxies as a separate virtual-adapter/Windows-filtering-driver milestone, with its own compatibility and recovery work.
- Define behavior for applications that ignore the configured proxy and for long-lived connections that cannot move after they start.
- Keep the product claim precise: separate connections can use separate uplinks; splitting one TCP connection across both links requires a remote bonding endpoint.

## 2. Safe activation and recovery

- Keep startup and passive mode read-only. Only an explicit Boost action may activate the engine.
- For system proxy takeover, encrypt a recovery record with Windows DPAPI and restore only per-user proxy values that still match NetFusion's own values. NetFusion does not modify routes or DNS.
- Restore state on Stop, link loss, application exit, failed startup, and reboot recovery. Never overwrite changes the user or another VPN made after Boost started.
- Preserve existing proxy/PAC/auto-detect settings and expose local proxy ports for manual setup instead of taking over an already-active proxy.
- Boost is blocked while a WARP adapter is active until coexistence and privacy behavior are tested. Then validate other VPNs and virtual adapters too.

## 3. Compatibility and reliability

- Test with Ethernet plus Wi-Fi, each link failing during traffic, sleep/resume, sign-in startup, and repeated start/stop cycles.
- Test Cloudflare WARP and other VPN/proxy combinations in the supported active mode, and clearly report unsupported combinations.
- Measure CPU and memory in the tray, during adapter polling, and while Boost is active.
- Test the installer and uninstall/upgrade paths on a clean Windows 10/11 x64 machine; sign the public installer and provide a license and support contact before a production release.

## Current milestone

The supplied app icon is integrated, adapter polling runs off the UI thread, and the local HTTP, HTTPS CONNECT, and SOCKS5 proxy paths pass loopback checks. Mocked Windows proxy takeover/restoration, recovery, and preservation checks pass; the x64 installer compiles from this source revision. The downloadable `v0.1.0` release still represents the original monitor build. Before a stable release, validate real traffic across both uplinks, Windows proxy behavior across applications, link loss on real adapters, Steam, installer launch on a clean machine, and active WARP behavior. Publish a new version only after its source and installer have been built and verified together.

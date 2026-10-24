# NetFusion release status

## v1.0.1 build

The source and x64 installer are versioned `1.0.1`. This patch keeps sessions for a destination host on the same adapter to reduce failures when browser connections would otherwise use different ISP addresses. It also includes passive startup monitoring, explicit Boost activation, per-socket adapter pinning for local HTTP/HTTPS and SOCKS5 proxy sessions, and guarded proxy recovery. The x64 installer builds successfully on Windows 11 with Python 3.14 and Inno Setup 6.

Automated coverage currently includes 17 tests for proxy protocols, relay shutdown, adapter state transitions, and mocked Windows proxy setting recovery. Live direct and proxied HTTPS requests also succeeded through both connected uplinks on the developer machine.

## Remaining release validation

- Verify active proxy restoration against real Windows proxy settings on a clean test account or VM; current lifecycle tests use a mocked registry.
- Verify Boost start/stop, link loss, sign-in startup, and uninstall/upgrade behavior on a clean Windows 10/11 x64 machine.
- Confirm which real applications honor the proxy, especially Steam. Apps that ignore the Windows proxy will not use NetFusion automatically.
- Cloudflare WARP is blocked during Boost until active coexistence is tested. Passive mode leaves it alone.
- Measure CPU and memory over a longer tray session and while multiple proxy sessions are active.
- Sign the installer and provide a support contact before broad public distribution.

The engine distributes separate TCP connections; it does not split a single connection across links. True single-flow bonding generally requires a remote aggregation server and a compatible client protocol.

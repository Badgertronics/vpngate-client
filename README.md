# VPN Gate Client

A graphical VPN Gate client for Linux desktops (built for Ubuntu 24/26 with
GNOME/Wayland in mind). It downloads the daily relay list, presents it in a
searchable, filterable table and connects with the protocols the relays
actually speak - SSL-VPN (SoftEther), MS-SSTP and L2TP/IPsec are working
implementations; OpenVPN and PPTP are present but usually unusable for other
reasons (see "Protocol status").

The heavy lifting runs in one long-lived **root worker** process that talks
JSON lines over stdin/stdout, so the desktop application itself stays
unprivileged and the user is asked for administrative rights only once, via
`pkexec` (polkit) with a `sudo` fallback.

## Protocol status (practically, in 2026)

| Protocol    | Status                                                      |
|-------------|-------------------------------------------------------------|
| SSL-VPN     | **Works** (SoftEther) - most reliable protocol              |
| L2TP/IPsec  | **Works** - needs the checksum fixer; IKE-timeout relays exist |
| MS-SSTP     | **Works**                                                   |
| OpenVPN     | Blocked by a TLS wall on many relay links; kept for completeness |
| PPTP        | No VPN Gate relays serve PPTP any more                      |

Relay availability is volatile: many servers run today, die this afternoon.
JP/KR/TH relays generally survive censorship better than most. When a
connection fails because the server's virtual DHCP (SecureNAT) did not
answer, the client tells you so - just try the next server.

## Install

On the target machine:

    sudo ./app/install.sh

This installs the app to `~/VPNGate`, all required packages
(`python3-gi`, `python3-gi-cairo`, `gir1.2-gtk-4.0`, `gir1.2-adw-1`,
`openvpn`, `strongswan-swanctl`, `xl2tpd`, `sstp-client`, `ppp`,
`python3-netfilterqueue`, `softether-vpnclient` …), the interpreter/cairo
bridge needed for the animated widgets, the **application + tray icons**
(installed into the hicolor icon theme) and the desktop entry ("VPN Gate
Client" in the application menu, `Icon=com.vpngate.client`), plus the TUN
module autoload. Tray bindings (`libayatana-appindicator3`) are installed
op-portunistically; if they are missing the client still works, just
without the tray icon.

## Run

From the menu ("VPN Gate Client"), or directly:

    ~/VPNGate/vpngate_gui.py

1. The server list downloads automatically (repository mirrors first, then
   the compiled-in mirrors, then this repo's `servers.csv`, then the CDN
   snapshot when everything else is unreachable).
2. Pick a row (JP/KR/TH first is a good heuristic), select a protocol
   (SSL-VPN is the default), press **Connect** and authorise once: polkit
   (`pkexec`) asks through its own desktop dialog; if no polkit agent is
   available in your session the client asks for your user password and
   uses `sudo` instead.
3. The status bar shows the tunnel interface, tunnel IP, your public IP as
   seen through the tunnel and the session time.

## Layout

    app/
      vpngate_gui.py        GUI entry point
      worker_main.py        privileged worker entry point (as root)
      install.sh            installer
      icons/hicolor/...     application + tray SVG icons
      vpngate/
        ui.py               GTK4 + libadwaita interface
        widgets.py          stylesheet + animated widgets (StatusRing, chips…)
        tray.py             tray parent (JSON over pipes)
        tray_worker.py      isolated GTK3 tray child (libayatana)
        worker.py           JSON-lines command loop (connect/disconnect/status)
        privilege.py        pkexec / sudo spawning
        fetcher.py          mirror scraping + repo/CDN CSV fallbacks + parsers
        models.py           VpnServer dataclass + Protocol enum
        backends.py         SoftEther/SSL-VPN, L2TP/IPsec, MS-SSTP, OpenVPN
        netutil.py          DHCP client, routing, interface helpers
        csumfix.py          NFQUEUE ESP-in-UDP checksum fixer (L2TP)
        config.py           mirrors, credentials, timeouts, donation wallets
    scripts/
      update_data.py        refreshes data/ (also runnable locally)
    data/
      mirrors.json          daily mirror list (committed by the Action)
      servers.csv           daily copy of the official relay CSV

`_tools/` on the development machine is the private SSH harness used to
build and verify the client against a real machine; it holds remote
credentials and is therefore **not** part of the published tree.

## Relay data refresh (GitHub Action)

`.github/workflows/refresh-mirrors.yml` runs `scripts/update_data.py`
every 10 minutes (and can be triggered manually from the Actions
tab). It

1. scrapes the mirror list from
   https://www.vpngate.net/en/sites.aspx into `data/mirrors.json`, and
2. downloads the official relay CSV from
   https://www.vpngate.net/api/iphone/ into `data/servers.csv`,

then commits any change as
`chore(data): refresh daily mirror list + relay CSV [skip ci]`.
Both files can also be refreshed locally with `python scripts/update_data.py`.

## Building & Releases (CI / CD)

Precompiled packages are built and published automatically by
`.github/workflows/release.yml` on every tag release (e.g. `v1.0.0`) or manual
workflow dispatch:
- **Linux x86_64 (`amd64`)**: Standalone `.tar.gz` and Debian `.deb` package
- **Linux ARM64 (`aarch64`)**: Standalone `.tar.gz` and Debian `.deb` package
- **Windows x86_64**: Standalone `.zip` package

To build release packages locally:

    # Build standalone tarball (and .deb on Debian/Ubuntu):
    python scripts/build.py --deb

    # Output files will be located in dist_release/

The client reads them online — no rebuild needed: when the mirrors it knows
are unreachable, it fetches `data/mirrors.json` (fresh mirrors) and, failing
that, `data/servers.csv` (the whole server table) via
`raw.githubusercontent.com` and the jsDelivr CDN copies of this repository.

## Notes for users behind censorship

- If **every** mirror returns HTTP 500 / TLS resets, the list still arrives
  from the CDN snapshot; connections themselves may also be filtered - VPN
  Gate traffic is not stealth traffic, a charting firewall can detect it.
- L2TP needs the checksum fixer only when the tunnel passes routers that
  rewrite ESP-in-UDP packets badly (very common on home routers); the
  client installs and removes the fixer automatically per session.
- If the same server repeatedly fails its DHCP phase, its SecureNAT is
  dead - not a client problem; rotate servers.

## ☕ Buy me a coffee

If this client saves you when the open internet is a brick wall away,
a coffee is always appreciated. Send any amount to one of these wallets
(copy-paste friendly):

| Network                     | Address                                |
|-----------------------------|----------------------------------------|
| Bitcoin (BTC)               | `bc1q3l2gxktlc3r9f6z325d920kqhv8kwctqdyvxmh`          |
| Ethereum (ETH / ERC-20)     | `0x9C2Cde3575769e104E93bC934abbfE2D883e8dCa`          |
| BEP20 (BNB Smart Chain)     | `0x9C2Cde3575769e104E93bC934abbfE2D883e8dCa`        |

---

*VPN Gate Client* — built by **Mr. Honey Badger (Badgertronics)**.
 VPN Gate is an academic experiment by the University of Tsukuba;
this client is an independent desktop GUI for its public relays.

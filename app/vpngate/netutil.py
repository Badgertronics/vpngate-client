"""Small privileged helpers: routing, DHCP and address discovery."""
from __future__ import annotations

import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------


def run(cmd: List[str], timeout: int = 30, check: bool = False) -> Tuple[int, str, str]:
    """Run *cmd* and return (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {proc.stderr.strip()}")
    return proc.returncode, proc.stdout, proc.stderr


def _ip_path() -> str:
    return shutil.which("ip") or "/usr/sbin/ip"


def ip_cmd(*args: str, timeout: int = 15) -> Tuple[int, str, str]:
    if sys.platform == "win32":
        return 1, "", "ip command is not available on Windows"
    return run([_ip_path(), *args], timeout=timeout)


# ---------------------------------------------------------------------------
# Interface / route inspection
# ---------------------------------------------------------------------------


def interface_ipv4(iface: str) -> Optional[str]:
    code, out, _ = ip_cmd("-4", "-o", "addr", "show", "dev", iface)
    if code != 0:
        return None
    m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else None


def interface_peer_ipv4(iface: str) -> Optional[str]:
    """Return the peer address of a point to point interface (ppp*)."""
    code, out, _ = ip_cmd("-4", "-o", "addr", "show", "dev", iface)
    if code != 0:
        return None
    m = re.search(r"peer\s+(\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else None


def interface_up(iface: str) -> bool:
    code, out, _ = ip_cmd("link", "show", "dev", iface)
    if code != 0:
        return False
    return "state UP" in out or "state UNKNOWN" in out


def route_via(iface: str) -> Optional[str]:
    """Default gateway that is reachable through *iface*."""
    code, out, _ = ip_cmd("-4", "route", "show", "dev", iface)
    if code != 0:
        return None
    for line in out.splitlines():
        if line.startswith("default via"):
            parts = line.split()
            if "via" in parts:
                return parts[parts.index("via") + 1]
    return None


def list_tunnel_interfaces() -> List[str]:
    code, out, _ = ip_cmd("-o", "link", "show")
    if code != 0:
        return []
    names = []
    for line in out.splitlines():
        m = re.match(r"\d+:\s+([^:@]+)", line)
        if not m:
            continue
        name = m.group(1).strip()
        if name.startswith("tun") or name.startswith("ppp") or name.startswith("vpn_"):
            names.append(name)
    return names


def default_routes() -> List[Tuple[str, str, int]]:
    """Return [(gateway, device, metric)] for every default route."""
    if sys.platform == "win32":
        code, out, _ = run(["route", "print", "0.0.0.0"])
        if code != 0:
            return []
        routes = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
                gw = parts[2]
                iface = parts[3]
                try:
                    metric = int(parts[4])
                except ValueError:
                    metric = 0
                routes.append((gw, iface, metric))
        return routes

    code, out, _ = ip_cmd("-4", "route", "show", "default")
    if code != 0:
        return []
    routes = []
    for line in out.splitlines():
        parts = line.split()
        if "via" not in parts:
            continue
        gateway = parts[parts.index("via") + 1]
        device = parts[parts.index("dev") + 1] if "dev" in parts else ""
        metric = 0
        if "metric" in parts:
            try:
                metric = int(parts[parts.index("metric") + 1])
            except ValueError:
                metric = 0
        routes.append((gateway, device, metric))
    return routes


# ---------------------------------------------------------------------------
# Route manipulation
# ---------------------------------------------------------------------------


def install_tunnel_default(gateway: str, iface: str, server_ip: str = "",
                            local_gateway: str = "") -> None:
    """Point the default route at the tunnel.

    A host route towards the VPN server is kept on the physical link so the
    tunnel does not swallow its own transport traffic.
    """
    if local_gateway:
        # Remember the physical gateway so the server stays reachable.
        if server_ip:
            ip_cmd("route", "replace", server_ip, "via", local_gateway,
                   "dev", _physical_device() or "lo")
    ip_cmd("route", "replace", "default", "via", gateway, "dev", iface,
           "metric", "10")


def remove_tunnel_default(gateway: str, iface: str) -> None:
    ip_cmd("route", "del", "default", "via", gateway, "dev", iface)


def _physical_device() -> str:
    code, out, _ = ip_cmd("-4", "route", "show", "default")
    if code != 0:
        return ""
    for line in out.splitlines():
        parts = line.split()
        if "dev" in parts:
            return parts[parts.index("dev") + 1]
    return ""


# ---------------------------------------------------------------------------
# Minimal DHCP client
# ---------------------------------------------------------------------------

_DHCP_MAGIC = b"\x63\x82\x53\x63"


def _dhcp_packet(kind: int, mac: bytes, xid: int, yiaddr: bytes = b"\0" * 4,
                 options: bytes = b"") -> bytes:
    chaddr = mac + b"\0" * 10
    pkt = struct.pack("!BBBBIHH", kind, 1, 6, 0, xid, 0, 0x8000)
    pkt += chaddr + b"\0" * 64 + b"\0" * 128 + _DHCP_MAGIC + options
    return pkt.ljust(300, b"\0")


def _dhcp_options(data: bytes) -> dict:
    opts, i = {}, 0
    while i < len(data):
        code = data[i]
        if code == 255:
            break
        if code == 0:
            i += 1
            continue
        if i + 1 >= len(data):
            break
        length = data[i + 1]
        opts[code] = data[i + 2:i + 2 + length]
        i += 2 + length
    return opts


def dhcp_request(iface: str, timeout: float = 30.0) -> Optional[dict]:
    """Perform a DHCP DISCOVER/OFFER/REQUEST/ACK exchange on *iface*.

    Returns a dict with ``ip``, ``netmask``, ``gateway`` and ``dns`` keys or
    ``None`` when the server did not answer.
    """
    try:
        with open(f"/sys/class/net/{iface}/address") as handle:
            mac = bytes(int(part, 16) for part in handle.read().strip().split(":"))
    except OSError:
        return None

    xid = int(time.time() * 1000) & 0xFFFFFFFF
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                            iface.encode() + b"\0")
        except OSError:
            pass
        sock.bind(("", 68))
        sock.settimeout(4)

        client_id = b"\x3d\x07\x01" + mac
        sock.sendto(_dhcp_packet(1, mac, xid,
                                 options=b"\x35\x01\x01" + client_id + b"\xff"),
                    ("255.255.255.255", 67))

        deadline = time.time() + timeout
        yiaddr = None
        while time.time() < deadline:
            try:
                data, _ = sock.recvfrom(2048)
            except socket.timeout:
                break
            if len(data) < 240 or data[0] != 2:
                continue
            opts = _dhcp_options(data[240:])
            if opts.get(53) != b"\x02":
                continue
            yiaddr = data[16:20]
            break
        if not yiaddr:
            return None

        sock.sendto(_dhcp_packet(3, mac, xid, yiaddr,
                                 options=b"\x35\x01\x03" + client_id +
                                         b"\x32\x04" + yiaddr + b"\xff"),
                    ("255.255.255.255", 67))

        while time.time() < deadline:
            try:
                data, _ = sock.recvfrom(2048)
            except socket.timeout:
                break
            if len(data) < 240 or data[0] != 2:
                continue
            opts = _dhcp_options(data[240:])
            if opts.get(53) != b"\x05":
                continue
            yiaddr = data[16:20]
            netmask = opts.get(1, b"\xff\x00\x00\x00")
            router = opts.get(3)
            dns = opts.get(6)
            return {
                "ip": socket.inet_ntoa(yiaddr),
                "netmask": socket.inet_ntoa(netmask),
                "gateway": socket.inet_ntoa(router) if router else None,
                "dns": [socket.inet_ntoa(dns[i:i + 4])
                        for i in range(0, len(dns), 4)] if dns else [],
            }
        return None
    finally:
        sock.close()


def configure_interface(iface: str, lease: dict) -> None:
    """Apply a DHCP lease to *iface*."""
    ip_cmd("addr", "replace", f"{lease['ip']}/{_netmask_prefix(lease['netmask'])}",
           "dev", iface)


def _netmask_prefix(netmask: str) -> int:
    try:
        return sum(bin(int(octet)).count("1") for octet in netmask.split("."))
    except ValueError:
        return 24


# ---------------------------------------------------------------------------
# External address discovery
# ---------------------------------------------------------------------------

_IP_SERVICES = [
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
    "http://checkip.amazonaws.com",
]


def external_ip(timeout: int = 12) -> str:
    """Return the public IP address seen from this host."""
    import urllib.request
    for url in _IP_SERVICES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                value = resp.read().decode("utf-8", "replace").strip()
            if re.match(r"^\d+\.\d+\.\d+\.\d+$", value):
                return value
        except Exception:                          # noqa: BLE001
            continue
    return ""


def which(program: str) -> str:
    return shutil.which(program) or ""

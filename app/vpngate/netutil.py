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
from typing import Callable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------


def clean_system_env() -> dict:
    """Return a clean environment dict without PyInstaller's LD_LIBRARY_PATH.

    When running inside a PyInstaller bundle on Linux, LD_LIBRARY_PATH points to
    bundled private libraries (like libcrypto.so.3). External system programs
    (e.g. strongswan/ipsec, xl2tpd, openvpn, curl) must use the OS's native libraries,
    otherwise symbols like OPENSSL_3.3.0 fail to resolve.
    """
    env = dict(os.environ)
    if "LD_LIBRARY_PATH_ORIG" in env:
        env["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH_ORIG"]
    else:
        env.pop("LD_LIBRARY_PATH", None)
    return env


def run(cmd: List[str], timeout: int = 30, check: bool = False, env: Optional[dict] = None) -> Tuple[int, str, str]:
    """Run *cmd* and return (returncode, stdout, stderr)."""
    if env is None and sys.platform != "win32":
        env = clean_system_env()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
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


def windows_physical_gateway() -> str:
    """Find the IPv4 gateway of the underlying physical network connection on Windows."""
    if sys.platform != "win32":
        return ""
    # 1. First check if default_routes() has a non-virtual/non-VPN gateway
    for gw, _iface, _metric in default_routes():
        if gw and gw != "0.0.0.0" and not gw.startswith("10.211.") and not gw.startswith("10.8.") and not gw.startswith("169.254."):
            return gw
    # 2. Check route print for any gateway used on physical LANs
    code, out, _ = run(["route", "print"])
    if code == 0:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[1] == "255.255.255.255":
                gw = parts[2]
                if (gw.startswith("192.168.") or gw.startswith("10.") or gw.startswith("172.")) and not gw.startswith("10.211."):
                    return gw
    # 3. Check ipconfig for IPv4 Address and derive .1 gateway
    code, out, _ = run(["ipconfig"])
    if code == 0:
        for line in out.splitlines():
            m = re.search(r"IPv4 Address[ .:]+:\s*(\d+\.\d+\.\d+)\.\d+", line)
            if m:
                prefix = m.group(1)
                if not prefix.startswith("10.211.") and not prefix.startswith("169.254."):
                    return f"{prefix}.1"
    return ""


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


def measure_server_latency(server, timeout: float = 0.8) -> int:
    """Measure real network latency to a VPN server in milliseconds.

    Executes a fast in-process TCP SYN handshake directly to the VPN port
    (SSL-VPN / SSTP / OpenVPN or 443). If TCP is unresponsive, performs a fast
    ICMP echo ping.
    Returns latency in milliseconds (>= 1), or -1 if unreachable / timed out.
    """
    ip = getattr(server, "ip", "") or ""
    if not ip:
        return -1

    # 1. Fast direct TCP socket handshake on active VPN ports (in-process, low latency)
    ports: List[int] = []
    sstp_port = getattr(server, "sstp_port", None)
    if sstp_port:
        try:
            ports.append(int(sstp_port))
        except (ValueError, TypeError):
            pass
    ssl_vpn_port = getattr(server, "ssl_vpn_port", None)
    if ssl_vpn_port:
        try:
            p_int = int(ssl_vpn_port)
            if p_int not in ports:
                ports.append(p_int)
        except (ValueError, TypeError):
            pass
    openvpn_tcp = getattr(server, "openvpn_tcp", None)
    if openvpn_tcp:
        try:
            p_int = int(openvpn_tcp)
            if p_int not in ports:
                ports.append(p_int)
        except (ValueError, TypeError):
            pass
    if 443 not in ports:
        ports.append(443)

    tcp_timeout = min(timeout, 0.7)
    for p in ports[:2]:
        t0 = time.perf_counter()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(tcp_timeout)
            err = s.connect_ex((ip, p))
            s.close()
            dt = int((time.perf_counter() - t0) * 1000)
            # 0 = connected; 10061 (Win) or 111 (Linux) = connection refused (host is alive and replied with RST)
            if err in (0, 10061, 111):
                return max(1, dt)
        except Exception:
            pass

    # 2. Fast ICMP fallback (0.6s)
    try:
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        icmp_timeout = min(timeout, 0.6)
        if sys.platform == "win32":
            cmd = ["ping", "-n", "1", "-w", str(int(icmp_timeout * 1000)), ip]
        else:
            cmd = ["ping", "-c", "1", "-W", "1", ip]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=icmp_timeout + 0.5,
                             creationflags=creation_flags)
        if res.returncode == 0:
            out = res.stdout.lower()
            m = re.search(r"time[=<]([\d.]+)\s*ms", out)
            if m:
                return max(1, int(float(m.group(1))))
    except Exception:
        pass

    return -1


def format_size(bytes_num: float) -> str:
    """Format bytes into human-readable B, KB, MB, or GB."""
    if bytes_num >= 1024 * 1024 * 1024:
        return f"{bytes_num / (1024 * 1024 * 1024):.2f} GB"
    elif bytes_num >= 1024 * 1024:
        return f"{bytes_num / (1024 * 1024):.1f} MB"
    elif bytes_num >= 1024:
        return f"{bytes_num / 1024:.1f} KB"
    else:
        return f"{int(bytes_num)} B"


def format_time(seconds: float) -> str:
    """Format seconds into MM:SS or HH:MM:SS."""
    if seconds < 0 or seconds > 86400 * 7:
        return "--:--"
    sec = int(round(seconds))
    mins, s = divmod(sec, 60)
    hrs, m = divmod(mins, 60)
    if hrs > 0:
        return f"{hrs:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _format_download_progress(prefix: str, downloaded: int, total: int, speed: float,
                              elapsed: float, eta: float) -> str:
    spd_str = f"{format_size(speed)}/s" if speed > 0 else "0 B/s"
    elap_str = format_time(elapsed)
    if total > 0:
        pct = min(100.0, (downloaded / total) * 100.0)
        rem_bytes = max(0, total - downloaded)
        eta_str = format_time(eta) if eta >= 0 else "--:--"
        return (
            f"{prefix} Progress: {pct:.1f}% | "
            f"Downloaded: {format_size(downloaded)} / {format_size(total)} | "
            f"Remaining size: {format_size(rem_bytes)} | "
            f"Speed: {spd_str} | "
            f"Elapsed: {elap_str} | "
            f"Remaining time: {eta_str}"
        )
    else:
        return (
            f"{prefix} Downloaded: {format_size(downloaded)} | "
            f"Speed: {spd_str} | "
            f"Elapsed: {elap_str}"
        )


def download_file_resilient(url: str, dest_path: str, timeout: int = 180,
                            expected_size: int = 0,
                            log_cb: Optional[Callable[[str], None]] = None,
                            prefix: str = "[download]") -> bool:
    """Download a file with live progress reporting and multi-stage fallback.

    Tracks and logs: progress %, downloaded size, remaining size, download speed,
    elapsed time, and remaining time (ETA).
    Tries Python urllib streaming with unverified SSL first, then native curl with
    size-monitoring, and finally PowerShell on Windows.
    """
    def _log(msg: str) -> None:
        if log_cb:
            log_cb(msg)

    # Clean existing destination if any
    try:
        if os.path.exists(dest_path):
            os.remove(dest_path)
    except Exception:
        pass

    # 1. Primary: Python urllib streaming in chunks with resilient SSL context
    try:
        import urllib.request
        import ssl
        ctx = ssl._create_unverified_context()
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VPN-Gate-Client"}
        )
        with urllib.request.urlopen(req, context=ctx, timeout=min(timeout, 30)) as resp:
            total_size = expected_size
            try:
                cl = resp.headers.get("Content-Length")
                if cl:
                    cl_val = int(cl.strip())
                    if cl_val > 0:
                        total_size = cl_val
            except Exception:
                pass

            t0 = time.perf_counter()
            last_report = t0
            last_bytes = 0
            downloaded = 0
            smoothed_speed = 0.0
            chunk_size = 65536

            with open(dest_path, "wb") as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)

                    now = time.perf_counter()
                    if now - last_report >= 0.8:
                        delta_t = now - last_report
                        delta_b = downloaded - last_bytes
                        inst_speed = delta_b / delta_t if delta_t > 0 else 0.0
                        smoothed_speed = inst_speed if smoothed_speed == 0.0 else (0.4 * inst_speed + 0.6 * smoothed_speed)
                        elapsed = now - t0
                        rem_bytes = max(0, total_size - downloaded) if total_size > 0 else 0
                        eta = rem_bytes / smoothed_speed if (smoothed_speed > 1024 and total_size > 0) else -1
                        _log(_format_download_progress(prefix, downloaded, total_size, smoothed_speed, elapsed, eta))
                        last_report = now
                        last_bytes = downloaded

            if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
                final_size = os.path.getsize(dest_path)
                total_elapsed = max(0.001, time.perf_counter() - t0)
                avg_speed = final_size / total_elapsed
                _log(_format_download_progress(prefix, final_size, total_size or final_size, avg_speed, total_elapsed, 0.0))
                _log(f"{prefix} ✓ Download completed: {format_size(final_size)} in {format_time(total_elapsed)} (avg {format_size(avg_speed)}/s)")
                return True
    except Exception as exc:
        _log(f"{prefix} urllib attempt encountered: {exc}, trying curl ...")

    # 2. Fallback: native curl binary with live file-size progress monitoring
    curl_candidates = [
        "curl.exe" if sys.platform == "win32" else "curl",
        "C:\\Windows\\System32\\curl.exe",
        shutil.which("curl.exe") if sys.platform == "win32" else shutil.which("curl"),
    ]
    curl_bin = next((c for c in curl_candidates if c and (os.path.isfile(c) or shutil.which(c))), None)

    if curl_bin:
        try:
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            cmd = [
                curl_bin, "-L", "-k", "--fail", "--retry", "2",
                "--connect-timeout", "15", "-m", str(timeout),
                "-o", dest_path, url
            ]
            t0 = time.perf_counter()
            last_report = t0
            last_bytes = 0
            smoothed_speed = 0.0
            total_size = expected_size

            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=creation_flags)
            while proc.poll() is None:
                time.sleep(0.8)
                if os.path.exists(dest_path):
                    downloaded = os.path.getsize(dest_path)
                    now = time.perf_counter()
                    delta_t = now - last_report
                    delta_b = downloaded - last_bytes
                    inst_speed = delta_b / delta_t if delta_t > 0 else 0.0
                    smoothed_speed = inst_speed if smoothed_speed == 0.0 else (0.4 * inst_speed + 0.6 * smoothed_speed)
                    elapsed = now - t0
                    rem_bytes = max(0, total_size - downloaded) if total_size > 0 else 0
                    eta = rem_bytes / smoothed_speed if (smoothed_speed > 1024 and total_size > 0) else -1
                    _log(_format_download_progress(prefix, downloaded, total_size, smoothed_speed, elapsed, eta))
                    last_report = now
                    last_bytes = downloaded

            if proc.returncode == 0 and os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
                final_size = os.path.getsize(dest_path)
                total_elapsed = max(0.001, time.perf_counter() - t0)
                avg_speed = final_size / total_elapsed
                _log(_format_download_progress(prefix, final_size, total_size or final_size, avg_speed, total_elapsed, 0.0))
                _log(f"{prefix} ✓ Download completed: {format_size(final_size)} in {format_time(total_elapsed)} (avg {format_size(avg_speed)}/s)")
                return True
        except Exception as exc:
            _log(f"{prefix} curl attempt failed: {exc}")

    # 3. Windows PowerShell WebClient fallback
    if sys.platform == "win32":
        try:
            ps_cmd = (
                "[Net.ServicePointManager]::SecurityProtocol = "
                "[Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13; "
                f"(New-Object Net.WebClient).DownloadFile('{url}', '{dest_path}')"
            )
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            t0 = time.perf_counter()
            last_report = t0
            last_bytes = 0
            smoothed_speed = 0.0
            total_size = expected_size

            proc = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=creation_flags)
            while proc.poll() is None:
                time.sleep(0.8)
                if os.path.exists(dest_path):
                    downloaded = os.path.getsize(dest_path)
                    now = time.perf_counter()
                    delta_t = now - last_report
                    delta_b = downloaded - last_bytes
                    inst_speed = delta_b / delta_t if delta_t > 0 else 0.0
                    smoothed_speed = inst_speed if smoothed_speed == 0.0 else (0.4 * inst_speed + 0.6 * smoothed_speed)
                    elapsed = now - t0
                    rem_bytes = max(0, total_size - downloaded) if total_size > 0 else 0
                    eta = rem_bytes / smoothed_speed if (smoothed_speed > 1024 and total_size > 0) else -1
                    _log(_format_download_progress(prefix, downloaded, total_size, smoothed_speed, elapsed, eta))
                    last_report = now
                    last_bytes = downloaded

            if proc.returncode == 0 and os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
                final_size = os.path.getsize(dest_path)
                total_elapsed = max(0.001, time.perf_counter() - t0)
                avg_speed = final_size / total_elapsed
                _log(_format_download_progress(prefix, final_size, total_size or final_size, avg_speed, total_elapsed, 0.0))
                _log(f"{prefix} ✓ Download completed: {format_size(final_size)} in {format_time(total_elapsed)} (avg {format_size(avg_speed)}/s)")
                return True
        except Exception as exc:
            _log(f"{prefix} powershell attempt failed: {exc}")

    return False


def download_json_resilient(url: str, timeout: int = 15) -> Optional[dict]:
    """Fetch JSON with resilient SSL handling."""
    import json
    import ssl
    import urllib.request
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "VPN-Gate-Client",
                "Accept": "application/vnd.github.v3+json"
            }
        )
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            data = resp.read().decode("utf-8", "replace")
            return json.loads(data)
    except Exception:
        # Try curl if python urllib failed
        curl_candidates = [
            "curl.exe" if sys.platform == "win32" else "curl",
            "C:\\Windows\\System32\\curl.exe",
            shutil.which("curl.exe") if sys.platform == "win32" else shutil.which("curl"),
        ]
        curl_bin = next((c for c in curl_candidates if c and (os.path.isfile(c) or shutil.which(c))), None)
        if curl_bin:
            try:
                creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                cmd = [curl_bin, "-s", "-L", "-k", "--connect-timeout", "10", "-m", str(timeout), url]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 2, creationflags=creation_flags)
                if res.returncode == 0 and res.stdout:
                    return json.loads(res.stdout)
            except Exception:
                pass
    return None


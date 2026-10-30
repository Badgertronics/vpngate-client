"""Fetching and parsing the daily VPN Gate relay server list.

Two endpoints are used:

``<mirror>/en/``
    The HTML "Public VPN Relay Servers" table.  It is the only source that
    advertises which protocols a relay supports and on which ports.

``<mirror>/api/iphone/``
    The CSV feed.  It contains the same servers plus a base64 encoded
    ``.ovpn`` configuration for the OpenVPN service.
"""
from __future__ import annotations

import base64
import csv
import html
import io
import json
import re
import ssl
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

from .config import (REPO_CSV_URLS, REPO_MIRROR_LIST_URLS,  # noqa: E402
                     USER_AGENT)
from .models import VpnServer

# ---------------------------------------------------------------------------
# Low level helpers
# ---------------------------------------------------------------------------


def http_get(url: str, timeout: int = 45) -> bytes:
    """GET *url* and return the body, raising urllib errors on failure.

    Some mirrors refuse plain HTTP ("500 Server Error" from their HTTPS
    front) and some start serving behind TLS without certificate data,
    so a 5xx answer retries the request through https:// transparently.
    """
    try:
        return _http_get_plain(url, timeout)
    except urllib.error.HTTPError as exc:
        if url.startswith("http:") and 500 <= exc.code < 600:
            secure = "https:" + url[5:]
            return _http_get_plain(secure, timeout)
        raise


def _http_get_plain(url: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "close",
    })
    ctx = ssl.create_default_context()
    # Mirror certificates are frequently self-signed or expired; the
    # data we fetch is parsed, so transport authenticity is not the
    # deciding factor here.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read()


def _text(cell: str) -> str:
    """Strip tags from *cell* and collapse whitespace."""
    cell = re.sub(r"<br\s*/?>", " ", cell, flags=re.I)
    cell = re.sub(r"<[^>]+>", " ", cell)
    return re.sub(r"\s+", " ", html.unescape(cell)).strip()


def _int(value) -> int:
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0


def _float(value) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

_ROW_RE = re.compile(r"vg_table_row_[01]")
_CELL_RE = re.compile(
    r"<td\s+class='vg_table_row_[01]'[^>]*>(.*?)</td>", re.I | re.S)
_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.I | re.S)


def _server_tables(document: str) -> List[str]:
    """Return the candidate server tables, richest first."""
    tables = re.findall(r"<table[^>]*id=['\"]vg_hosts_table_id['\"][^>]*>(.*?)</table>",
                         document, re.I | re.S)
    scored = []
    for table in tables:
        rows = len(_ROW_RE.findall(table))
        if "DDNS hostname" in table:
            scored.append((rows, table))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [table for _, table in scored]


def _parse_country(cell: str) -> Tuple[str, str]:
    flag = re.search(r"flags/([A-Za-z0-9_\-]+)\.png", cell)
    code = ""
    if flag:
        code = flag.group(1).split("/")[-1].upper()
    name = _text(cell)
    if code and name.upper().startswith(code):
        name = name[len(code):].strip()
    return name, code


def _parse_host_cell(cell: str) -> Tuple[str, str, str]:
    """Return (ddns hostname, ip address, isp hostname)."""
    host = ""
    ip = ""
    isp = ""
    m = re.search(r"([A-Za-z0-9][A-Za-z0-9\-\.]*\.opengw\.net)", cell)
    if m:
        host = m.group(1)
    m = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", cell)
    if m:
        ip = m.group(1)
    m = re.search(r"\(([^)]{4,})\)", _text(cell))
    if m:
        isp = m.group(1)
    return host, ip, isp


def _parse_uptime_to_seconds(uptime_str: str) -> int:
    if not uptime_str:
        return 0
    m = re.search(r"(\d+)\s*(days?|hours?|mins?|minutes?|months?|weeks?)", uptime_str, re.I)
    if not m:
        return 0
    val = int(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("day"):
        return val * 86400
    if unit.startswith("hour"):
        return val * 3600
    if unit.startswith("min"):
        return val * 60
    if unit.startswith("week"):
        return val * 604800
    if unit.startswith("month"):
        return val * 2592000
    return val


def _parse_traffic_to_bytes(traffic_str: str) -> int:
    if not traffic_str:
        return 0
    m = re.search(r"([\d,]+\.?\d*)\s*(GB|MB|KB|TB|bytes)", traffic_str, re.I)
    if not m:
        return 0
    val = _float(m.group(1))
    unit = m.group(2).upper()
    if unit == "TB":
        return int(val * (1024**4))
    if unit == "GB":
        return int(val * (1024**3))
    if unit == "MB":
        return int(val * (1024**2))
    if unit == "KB":
        return int(val * 1024)
    return int(val)


def _parse_sessions(cell: str) -> Tuple[int, str, int]:
    text = _text(cell)
    m = re.search(r"([\d,]+)\s+sessions", text, re.I)
    sessions = _int(m.group(1)) if m else 0
    m = re.search(r"Total\s+([\d,]+)\s+users", text, re.I)
    users = _int(m.group(1)) if m else 0
    uptime = ""
    m = re.search(r"(\d+\s+(?:days|hours|mins|months|weeks))", text, re.I)
    if m:
        uptime = m.group(1)
    return sessions, uptime, users


def _parse_quality(cell: str) -> Tuple[float, int, str, str]:
    text = _text(cell)
    speed = 0.0
    m = re.search(r"([\d,]+\.?\d*)\s*Mbps", text, re.I)
    if m:
        speed = _float(m.group(1))
    ping = 0
    m = re.search(r"Ping:\s*([\d,]+)\s*ms", text, re.I)
    if m:
        ping = _int(m.group(1))
    traffic = ""
    m = re.search(r"([\d,]+\.?\d*)\s*GB", text, re.I)
    if m:
        traffic = m.group(1) + " GB"
    policy = ""
    m = re.search(r"Logging policy:\s*([A-Za-z0-9 \-]+)", text, re.I)
    if m:
        policy = m.group(1).strip()
    return speed, ping, traffic, policy


def _parse_sslvpn(cell: str) -> Optional[int]:
    text = _text(cell)
    m = re.search(r"TCP:\s*(\d+)", text, re.I)
    return _int(m.group(1)) if m else None


def _parse_l2tp(cell: str) -> bool:
    return "howto_l2tp.aspx" in cell.lower()


def _parse_openvpn(cell: str) -> Tuple[Optional[int], Optional[int], str, str]:
    """Return (tcp port, udp port, session id, host id)."""
    tcp = udp = None
    sid = hid = ""
    m = re.search(r"do_openvpn\.aspx\?([^'\"]+)", cell, re.I)
    if m:
        params = dict(re.findall(r"(\w+)=([^&]+)", html.unescape(m.group(1))))
        if params.get("tcp") and params["tcp"] != "0":
            tcp = _int(params["tcp"])
        if params.get("udp") and params["udp"] != "0":
            udp = _int(params["udp"])
        sid = params.get("sid", "")
        hid = params.get("hid", "")
    text = _text(cell)
    if tcp is None:
        m = re.search(r"TCP:\s*(\d+)", text, re.I)
        if m:
            tcp = _int(m.group(1))
    if udp is None:
        m = re.search(r"UDP:\s*(\d+)", text, re.I)
        if m and _int(m.group(1)):
            udp = _int(m.group(1))
    return tcp, udp, sid, hid


def _parse_sstp(cell: str) -> Tuple[str, Optional[int]]:
    text = _text(cell)
    m = re.search(r"SSTP Hostname\s*:\s*(\S+)", text, re.I)
    if not m:
        return "", None
    value = m.group(1)
    if ":" in value:
        host, _, port = value.rpartition(":")
        return host, _int(port)
    return value, None


def _parse_operator(cell: str) -> str:
    m = re.search(r"<i><b>(.*?)</b></i>", cell, re.I | re.S)
    if m:
        return _text(m.group(1))
    return _text(cell)


def parse_server_table(document: str, mirror: str = "") -> List[VpnServer]:
    """Parse the HTML server list into :class:`VpnServer` objects."""
    servers: List[VpnServer] = []
    seen = set()
    for table in _server_tables(document):
        for row_html in _TR_RE.findall(table):
            cells = _CELL_RE.findall(row_html)
            if len(cells) < 8:
                continue
            host, ip, isp = _parse_host_cell(cells[1])
            if not host and not ip:
                continue
            key = host or ip
            if key in seen:
                continue
            seen.add(key)

            country, code = _parse_country(cells[0])
            sessions, uptime, users = _parse_sessions(cells[2])
            speed, ping, traffic, policy = _parse_quality(cells[3])
            ssl_port = _parse_sslvpn(cells[4])
            l2tp = _parse_l2tp(cells[5])
            ovpn_tcp, ovpn_udp, ovpn_sid, ovpn_hid = _parse_openvpn(cells[6])
            sstp_host, sstp_port = _parse_sstp(cells[7])
            operator = _parse_operator(cells[8]) if len(cells) > 8 else ""
            score = 0
            if len(cells) > 9:
                m = re.search(r"([\d,]+)\s*</span>", cells[9])
                if m:
                    score = _int(m.group(1))

            if sstp_port is None and sstp_host:
                # The table omits the port when it is the default 443.
                sstp_port = ssl_port

            download_base = ""
            if mirror and ovpn_sid and ovpn_hid:
                download_base = (f"{mirror.rstrip('/')}/common/openvpn_download.aspx"
                                 f"?sid={ovpn_sid}&hid={ovpn_hid}")

            servers.append(VpnServer(
                country=country,
                country_code=code,
                hostname=host,
                ip=ip,
                isp_hostname=isp,
                sessions=sessions,
                uptime=uptime,
                uptime_seconds=_parse_uptime_to_seconds(uptime),
                total_users=users,
                speed_mbps=speed,
                ping_ms=ping,
                total_traffic=traffic,
                total_traffic_bytes=_parse_traffic_to_bytes(traffic),
                log_policy=policy,
                operator=operator,
                score=score,
                ssl_vpn_port=ssl_port,
                l2tp_supported=l2tp,
                openvpn_tcp=ovpn_tcp,
                openvpn_udp=ovpn_udp,
                sstp_host=sstp_host,
                sstp_port=sstp_port,
                openvpn_sid=ovpn_sid,
                openvpn_hid=ovpn_hid,
                openvpn_download_base=download_base,
            ))
        if servers:
            break
    return servers


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------


def parse_csv(csv_text: str) -> Dict[str, VpnServer]:
    """Parse the ``/api/iphone/`` CSV feed, keyed by DDNS hostname.

    The CSV feed uses the short host name (``public-vpn-76``) while the HTML
    table uses the fully qualified name (``public-vpn-76.opengw.net``), so
    the entries are indexed by both the short and the qualified name.
    """
    result: Dict[str, VpnServer] = {}
    reader = csv.reader(io.StringIO(csv_text))
    rows = [row for row in reader if row]
    for row in rows:
        if len(row) < 15 or not row[0] or row[0].startswith(("#", "*")):
            continue
        host, ip = row[0], row[1]
        uptime_ms = _int(row[8]) if len(row) > 8 else 0
        uptime_sec = uptime_ms // 1000 if uptime_ms > 0 else 0
        total_traffic_bytes = _int(row[10]) if len(row) > 10 else 0

        uptime_text = ""
        if uptime_sec >= 86400:
            uptime_text = f"{uptime_sec // 86400} days"
        elif uptime_sec >= 3600:
            uptime_text = f"{uptime_sec // 3600} hours"
        elif uptime_sec >= 60:
            uptime_text = f"{uptime_sec // 60} mins"

        traffic_text = ""
        if total_traffic_bytes >= (1024**4):
            traffic_text = f"{total_traffic_bytes / (1024**4):.2f} TB"
        elif total_traffic_bytes >= (1024**3):
            traffic_text = f"{total_traffic_bytes / (1024**3):.2f} GB"
        elif total_traffic_bytes >= (1024**2):
            traffic_text = f"{total_traffic_bytes / (1024**2):.2f} MB"

        server = VpnServer(
            country=row[5],
            country_code=row[6],
            hostname=host,
            ip=ip,
            score=_int(row[2]),
            ping_ms=_int(row[3]),
            speed_mbps=_float(row[4]) / 1_000_000.0,
            sessions=_int(row[7]),
            uptime=uptime_text,
            uptime_seconds=uptime_sec,
            total_users=_int(row[9]),
            total_traffic=traffic_text,
            total_traffic_bytes=total_traffic_bytes,
            log_policy=row[11],
            operator=row[12],
            openvpn_config_b64=row[14] or "",
        )
        result[host] = server
        if "." in host:
            result[host.split(".")[0]] = server
    return result


def merge_openvpn_configs(servers: List[VpnServer],
                          csv_map: Dict[str, VpnServer]) -> List[VpnServer]:
    """Copy the OpenVPN configuration and extra metadata from the CSV feed."""
    by_ip = {entry.ip: entry for entry in csv_map.values() if entry.ip}
    for server in servers:
        extra = csv_map.get(server.hostname)
        if extra is None and server.ip:
            extra = by_ip.get(server.ip)
        if extra is None:
            short = server.hostname.split(".")[0] if server.hostname else ""
            extra = csv_map.get(short)
        if not extra:
            continue
        server.openvpn_config_b64 = extra.openvpn_config_b64
        if not server.ping_ms:
            server.ping_ms = extra.ping_ms
        if not server.speed_mbps:
            server.speed_mbps = extra.speed_mbps
        if not server.operator:
            server.operator = extra.operator
        if extra.uptime_seconds and not server.uptime_seconds:
            server.uptime_seconds = extra.uptime_seconds
        if extra.total_traffic_bytes and not server.total_traffic_bytes:
            server.total_traffic_bytes = extra.total_traffic_bytes
        if not server.uptime and extra.uptime:
            server.uptime = extra.uptime
        if not server.total_traffic and extra.total_traffic:
            server.total_traffic = extra.total_traffic
        if not server.sessions and extra.sessions:
            server.sessions = extra.sessions
        if not server.total_users and extra.total_users:
            server.total_users = extra.total_users
    return servers


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class FetchError(Exception):
    pass


def fetch_from_mirror(mirror: str, want_csv: bool = True
                      ) -> Tuple[List[VpnServer], Dict[str, VpnServer]]:
    """Download and parse the server list of a single mirror site."""
    mirror = mirror.rstrip("/")
    base = mirror + "/en/"
    try:
        document = http_get(base).decode("utf-8", "replace")
    except Exception as exc:                      # noqa: BLE001
        raise FetchError(f"{mirror}: {exc}") from exc

    servers = parse_server_table(document, mirror)
    if not servers:
        raise FetchError(f"{mirror}: no server rows found")

    csv_map: Dict[str, VpnServer] = {}
    if want_csv:
        try:
            csv_map = parse_csv(
                http_get(mirror + "/api/iphone/").decode("utf-8", "replace"))
        except Exception:                          # noqa: BLE001
            csv_map = {}

    merge_openvpn_configs(servers, csv_map)
    return servers, csv_map


def fetch_servers(mirrors: List[str]) -> Tuple[List[VpnServer], str]:
    """Collect the relay table through a chain of increasingly robust sources.

    1. The fresh mirror list published in this project's own repository
       (``data/mirrors.json``, refreshed daily by the scheduled GitHub
       Action) — those mirrors are tried first.
    2. The mirrors compiled into the app / configured by the user.
    3. ``data/servers.csv`` from the project repository (the raw
       ``/api/iphone/`` CSV, fetched via raw.githubusercontent.com and its
       jsDelivr CDN copies).
    4. The hourly ``vpngate-list-mirror`` CSV snapshots on the jsDelivr
       CDN (third-party safety net).
    """
    errors = []
    candidates = list(mirrors)
    try:
        repo_mirrors, repo_url = fetch_repo_mirrors()
    except FetchError:
        pass
    except Exception as exc:                      # noqa: BLE001
        errors.append(f"repository mirror list: {exc}")
    else:
        merged = [m for m in mirrors if m not in repo_mirrors]
        candidates = repo_mirrors + merged

    for mirror in candidates:
        try:
            servers, _ = fetch_from_mirror(mirror)
        except FetchError as exc:
            errors.append(str(exc))
            continue
        except Exception as exc:                  # noqa: BLE001
            errors.append(f"{mirror}: {exc}")
            continue
        if servers:
            return servers, mirror
    # Every mirror was unreachable: use the committed CSV from the project
    # repository, then the hourly CDN snapshot as the very last resort.
    try:
        csv_text, source = fetch_repo_csv()
        servers = servers_from_csv(csv_text)
    except Exception as exc:                      # noqa: BLE001
        errors.append(f"repository servers.csv: {exc}")
    else:
        if servers:
            return servers, f"GitHub servers.csv via {source}"
    try:
        csv_text, source = fetch_fallback_csv()
        servers = servers_from_csv(csv_text)
    except Exception as exc:                      # noqa: BLE001
        errors.append(f"csv snapshot: {exc}")
    else:
        if servers:
            return servers, f"csv snapshot via {source}"
    raise FetchError("Could not reach any VPN Gate mirror:\n  " +
                     "\n  ".join(errors))


# ---------------------------------------------------------------------------
# Fallback data source: hourly CSV snapshots on the jsDelivr CDN
# ---------------------------------------------------------------------------


_FALLBACK_CSV_URLS = [
    "https://cdn.jsdelivr.net/gh/GeorgeXie2333/"
    "vpngate-list-mirror@latest/data/vpngate.csv",
    "https://fastly.jsdelivr.net/gh/GeorgeXie2333/"
    "vpngate-list-mirror@latest/data/vpngate.csv",
    "https://gcore.jsdelivr.net/gh/GeorgeXie2333/"
    "vpngate-list-mirror@latest/data/vpngate.csv",
    "https://testingcf.jsdelivr.net/gh/GeorgeXie2333/"
    "vpngate-list-mirror@latest/data/vpngate.csv",
    "https://quantil.jsdelivr.net/gh/GeorgeXie2333/"
    "vpngate-list-mirror@latest/data/vpngate.csv",
]


def fetch_fallback_csv() -> Tuple[str, str]:
    """Download a CSV snapshot of the relay table from the CDN."""
    last = ""
    for url in _FALLBACK_CSV_URLS:
        try:
            csv_text = http_get(url, timeout=60).decode("utf-8", "replace")
        except Exception as exc:                  # noqa: BLE001
            last = f"{url}: {exc}"
            continue
        if len(csv_text) > 40_000:                # sanity: non-empty snapshot
            return csv_text, url.split("//")[1].split("/")[0]
    raise FetchError(f"no CSV snapshot reachable ({last or 'no endpoints'})")


# ---------------------------------------------------------------------------
# Project repository data (published by the scheduled GitHub Action)
# ---------------------------------------------------------------------------


def fetch_repo_mirrors() -> Tuple[List[str], str]:
    """Read the fresh daily mirror list from this project's repository.

    ``data/mirrors.json`` is committed by ``refresh-mirrors.yml`` every day
    and holds the URLs scraped from ``vpngate.net/en/sites.aspx``.  Reading
    it through GitHub raw (and its jsDelivr CDN copies) gives the client an
    up-to-date mirror list even when the mirrors compiled into the app have
    all gone stale.
    """
    last = ""
    for url in REPO_MIRROR_LIST_URLS:
        try:
            blob = http_get(url, timeout=15).decode("utf-8", "replace")
            data = json.loads(blob)
        except Exception as exc:                  # noqa: BLE001
            last = f"{url}: {exc}"
            continue
        mirrors = [entry.rstrip("/") for entry in data.get("mirrors", [])
                   if isinstance(entry, str) and entry.startswith("http")]
        if mirrors:
            return mirrors, url.split("//")[1].split("/")[0]
    raise FetchError(f"repository mirror list unreachable ({last or 'no endpoints'})")


def fetch_repo_csv() -> Tuple[str, str]:
    """Read the committed copy of the official ``/api/iphone/`` CSV."""
    last = ""
    for url in REPO_CSV_URLS:
        try:
            csv_text = http_get(url, timeout=30).decode("utf-8", "replace")
        except Exception as exc:                  # noqa: BLE001
            last = f"{url}: {exc}"
            continue
        # sanity: the CSV carries the column header and real relay rows
        if len(csv_text) > 40_000 and "#HostName" in csv_text:
            return csv_text, url.split("//")[1].split("/")[0]
    raise FetchError(f"repository servers.csv unreachable ({last or 'no endpoints'})")


def servers_from_csv(csv_text: str) -> List[VpnServer]:
    """Build the relay table from the CSV feed alone.

    The official CSV carries no protocol columns, so they are inferred:
    the ``remote`` line inside each row's OpenVPN configuration points at
    a SoftEther listener that also speaks SSL-VPN, SSTP rides the HTTPS
    listener (443) and VPN Gate relays answer L2TP/IPsec on 1701.
    """
    csv_map = parse_csv(csv_text)
    servers: List[VpnServer] = []
    seen: set = set()
    for server in csv_map.values():
        if id(server) in seen:                    # short + FQ keys share objects
            continue
        seen.add(id(server))
        if server.hostname and "." not in server.hostname:
            server.hostname = server.hostname + ".opengw.net"
        port = _port_from_openvpn_config(server.openvpn_config_b64)
        server.ssl_vpn_port = port or 443
        if not server.l2tp_supported:
            server.l2tp_supported = True
        server.sstp_host = server.hostname or server.ip
        server.sstp_port = 443
        servers.append(server)
    servers.sort(key=lambda entry: entry.score, reverse=True)
    return servers


def _port_from_openvpn_config(b64: str) -> Optional[int]:
    """The ``remote`` port inside the per-row .ovpn blob."""
    if not b64:
        return None
    b64 = re.sub(r"\s+", "", b64)
    b64 += "=" * (-len(b64) % 4)
    try:
        config = base64.b64decode(b64).decode("utf-8", "replace")
    except Exception:                             # noqa: BLE001
        return None
    match = re.search(r"^\s*remote\s+\S+\s+(\d+)\s*$", config,
                      flags=re.MULTILINE)
    return int(match.group(1)) if match else None

"""Data model for VPN Gate relay servers."""
from __future__ import annotations

import html as _html
import re as _re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Protocol(str, Enum):
    """VPN Gate protocols supported by this application."""

    OPENVPN = "openvpn"
    L2TP = "l2tp"
    SSTP = "sstp"
    SSLVP = "sslvp"
    PPTP = "pptp"

    @property
    def label(self) -> str:
        return {
            Protocol.OPENVPN: "OpenVPN",
            Protocol.L2TP: "L2TP/IPsec",
            Protocol.SSTP: "MS-SSTP",
            Protocol.SSLVP: "SSL-VPN (SoftEther)",
            Protocol.PPTP: "PPTP",
        }[self]

    @property
    def short(self) -> str:
        return {
            Protocol.OPENVPN: "OpenVPN",
            Protocol.L2TP: "L2TP",
            Protocol.SSTP: "SSTP",
            Protocol.SSLVP: "SSL-VPN",
            Protocol.PPTP: "PPTP",
        }[self]


#: Order used when a server offers several protocols.  SSL-VPN comes
#: first because it is the most dependable protocol against VPN Gate
#: relays today, then L2TP/IPsec and MS-SSTP; OpenVPN and PPTP stay last
#: since most relays either no longer offer them or actively block them.
PROTOCOL_ORDER = [
    Protocol.SSLVP,
    Protocol.L2TP,
    Protocol.SSTP,
    Protocol.OPENVPN,
    Protocol.PPTP,
]


_BR_RE = _re.compile(r"<br\s*/?\s*>", _re.I)
_TAG_RE = _re.compile(r"<[^>]*>")


def _clean_str(value: str) -> str:
    """Make a mirror-provided string presentable.

    VPN Gate mirrors embed light HTML (``<br>`` tags, entities) in the
    country, operator and similar cells.  Drop the tags, unescape the
    entities and collapse whatever whitespace remains.
    """
    if "<" not in value and "&" not in value:
        return value
    value = _BR_RE.sub(" ", value)
    value = _TAG_RE.sub(" ", value)
    value = _html.unescape(value)
    return _re.sub(r"\s+", " ", value).strip()


#: Every field of :class:`VpnServer` that ends up on screen.
_KNOWN_TEXT_FIELDS = ("country", "country_code", "hostname", "ip",
                      "isp_hostname", "uptime", "total_traffic",
                      "log_policy", "operator", "sstp_host")


@dataclass
class VpnServer:
    """One row of the "Public VPN Relay Servers" table."""

    country: str = ""
    country_code: str = ""
    hostname: str = ""
    ip: str = ""
    isp_hostname: str = ""
    sessions: int = 0
    uptime: str = ""
    uptime_seconds: int = 0
    total_users: int = 0
    speed_mbps: float = 0.0
    ping_ms: int = 0
    total_traffic: str = ""
    total_traffic_bytes: int = 0
    log_policy: str = ""
    operator: str = ""
    score: int = 0

    # Protocol specific bits
    ssl_vpn_port: Optional[int] = None      # SoftEther / SSL-VPN TCP port
    l2tp_supported: bool = False
    openvpn_tcp: Optional[int] = None
    openvpn_udp: Optional[int] = None
    sstp_host: str = ""
    sstp_port: Optional[int] = None
    openvpn_config_b64: str = ""            # base64 .ovpn from the CSV API
    openvpn_download_base: str = ""         # per-server .ovpn download URL
    openvpn_sid: str = ""
    openvpn_hid: str = ""

    def __post_init__(self) -> None:
        # VpnServer(): never display raw mirror markup (``<br>``,
        # HTML entities) however the row reached us - the HTML table, the
        # CSV feed, a JSON cache or a worker payload.
        for name in _KNOWN_TEXT_FIELDS:
            value = getattr(self, name)
            if isinstance(value, str) and value and ("<" in value or "&" in value):
                setattr(self, name, _clean_str(value))

    @property
    def protocols(self):
        out = []
        if self.openvpn_tcp or self.openvpn_udp:
            out.append(Protocol.OPENVPN)
        if self.l2tp_supported:
            out.append(Protocol.L2TP)
        if self.sstp_port:
            out.append(Protocol.SSTP)
        if self.ssl_vpn_port:
            out.append(Protocol.SSLVP)
        out.sort(key=lambda p: PROTOCOL_ORDER.index(p))
        return out

    def supports(self, protocol: Protocol) -> bool:
        return protocol in self.protocols

    def port_for(self, protocol: Protocol):
        """Return the port to use for *protocol* (or None)."""
        if protocol is Protocol.OPENVPN:
            return self.openvpn_udp or self.openvpn_tcp
        if protocol is Protocol.SSLVP:
            return self.ssl_vpn_port
        if protocol is Protocol.SSTP:
            return self.sstp_port
        if protocol is Protocol.L2TP:
            return 1701
        if protocol is Protocol.PPTP:
            return 1723
        return None

    @property
    def speed_text(self) -> str:
        return f"{self.speed_mbps:.2f} Mbps" if self.speed_mbps else "-"

    @property
    def ping_text(self) -> str:
        return f"{self.ping_ms} ms" if self.ping_ms else "-"

    @property
    def sessions_text(self) -> str:
        return f"{self.sessions} sessions" if self.sessions else "-"

    @property
    def protocol_text(self) -> str:
        return ", ".join(p.short for p in self.protocols) or "-"

    def to_dict(self) -> dict:
        return {
            "country": self.country,
            "country_code": self.country_code,
            "hostname": self.hostname,
            "ip": self.ip,
            "isp_hostname": self.isp_hostname,
            "sessions": self.sessions,
            "uptime": self.uptime,
            "uptime_seconds": self.uptime_seconds,
            "total_users": self.total_users,
            "total_traffic": self.total_traffic,
            "total_traffic_bytes": self.total_traffic_bytes,
            "log_policy": self.log_policy,
            "operator": self.operator,
            "speed_mbps": self.speed_mbps,
            "ping_ms": self.ping_ms,
            "score": self.score,
            "ssl_vpn_port": self.ssl_vpn_port,
            "l2tp_supported": self.l2tp_supported,
            "openvpn_tcp": self.openvpn_tcp,
            "openvpn_udp": self.openvpn_udp,
            "sstp_host": self.sstp_host,
            "sstp_port": self.sstp_port,
            "openvpn_config_b64": self.openvpn_config_b64,
            "openvpn_download_base": self.openvpn_download_base,
            "openvpn_sid": self.openvpn_sid,
            "openvpn_hid": self.openvpn_hid,
        }


@dataclass
class ConnectionInfo:
    """Runtime information about an established tunnel."""

    protocol: str = ""
    server: str = ""
    ip: str = ""
    gateway: str = ""
    interface: str = ""
    started_at: float = 0.0
    detail: dict = field(default_factory=dict)

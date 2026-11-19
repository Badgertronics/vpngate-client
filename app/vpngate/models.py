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


COUNTRY_NAMES: dict[str, str] = {
    "AD": "Andorra",
    "AE": "United Arab Emirates",
    "AF": "Afghanistan",
    "AG": "Antigua and Barbuda",
    "AI": "Anguilla",
    "AL": "Albania",
    "AM": "Armenia",
    "AO": "Angola",
    "AQ": "Antarctica",
    "AR": "Argentina",
    "AS": "American Samoa",
    "AT": "Austria",
    "AU": "Australia",
    "AW": "Aruba",
    "AX": "Åland Islands",
    "AZ": "Azerbaijan",
    "BA": "Bosnia and Herzegovina",
    "BB": "Barbados",
    "BD": "Bangladesh",
    "BE": "Belgium",
    "BF": "Burkina Faso",
    "BG": "Bulgaria",
    "BH": "Bahrain",
    "BI": "Burundi",
    "BJ": "Benin",
    "BL": "Saint Barthélemy",
    "BM": "Bermuda",
    "BN": "Brunei",
    "BO": "Bolivia",
    "BQ": "Caribbean Netherlands",
    "BR": "Brazil",
    "BS": "Bahamas",
    "BT": "Bhutan",
    "BV": "Bouvet Island",
    "BW": "Botswana",
    "BY": "Belarus",
    "BZ": "Belize",
    "CA": "Canada",
    "CC": "Cocos (Keeling) Islands",
    "CD": "Congo, Democratic Republic of the",
    "CF": "Central African Republic",
    "CG": "Congo",
    "CH": "Switzerland",
    "CI": "Côte d'Ivoire",
    "CK": "Cook Islands",
    "CL": "Chile",
    "CM": "Cameroon",
    "CN": "China",
    "CO": "Colombia",
    "CR": "Costa Rica",
    "CU": "Cuba",
    "CV": "Cabo Verde",
    "CW": "Curaçao",
    "CX": "Christmas Island",
    "CY": "Cyprus",
    "CZ": "Czech Republic",
    "DE": "Germany",
    "DJ": "Djibouti",
    "DK": "Denmark",
    "DM": "Dominica",
    "DO": "Dominican Republic",
    "DZ": "Algeria",
    "EC": "Ecuador",
    "EE": "Estonia",
    "EG": "Egypt",
    "EH": "Western Sahara",
    "ER": "Eritrea",
    "ES": "Spain",
    "ET": "Ethiopia",
    "FI": "Finland",
    "FJ": "Fiji",
    "FK": "Falkland Islands",
    "FM": "Micronesia",
    "FO": "Faroe Islands",
    "FR": "France",
    "GA": "Gabon",
    "GB": "United Kingdom",
    "GD": "Grenada",
    "GE": "Georgia",
    "GF": "French Guiana",
    "GG": "Guernsey",
    "GH": "Ghana",
    "GI": "Gibraltar",
    "GL": "Greenland",
    "GM": "Gambia",
    "GN": "Guinea",
    "GP": "Guadeloupe",
    "GQ": "Equatorial Guinea",
    "GR": "Greece",
    "GS": "South Georgia and South Sandwich Islands",
    "GT": "Guatemala",
    "GU": "Guam",
    "GW": "Guinea-Bissau",
    "GY": "Guyana",
    "HK": "Hong Kong",
    "HM": "Heard Island and McDonald Islands",
    "HN": "Honduras",
    "HR": "Croatia",
    "HT": "Haiti",
    "HU": "Hungary",
    "ID": "Indonesia",
    "IE": "Ireland",
    "IL": "Israel",
    "IM": "Isle of Man",
    "IN": "India",
    "IO": "British Indian Ocean Territory",
    "IQ": "Iraq",
    "IR": "Iran",
    "IS": "Iceland",
    "IT": "Italy",
    "JE": "Jersey",
    "JM": "Jamaica",
    "JO": "Jordan",
    "JP": "Japan",
    "KE": "Kenya",
    "KG": "Kyrgyzstan",
    "KH": "Cambodia",
    "KI": "Kiribati",
    "KM": "Comoros",
    "KN": "Saint Kitts and Nevis",
    "KP": "North Korea",
    "KR": "Korea Republic",
    "KW": "Kuwait",
    "KY": "Cayman Islands",
    "KZ": "Kazakhstan",
    "LA": "Laos",
    "LB": "Lebanon",
    "LC": "Saint Lucia",
    "LI": "Liechtenstein",
    "LK": "Sri Lanka",
    "LR": "Liberia",
    "LS": "Lesotho",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "LV": "Latvia",
    "LY": "Libya",
    "MA": "Morocco",
    "MC": "Monaco",
    "MD": "Moldova",
    "ME": "Montenegro",
    "MF": "Saint Martin",
    "MG": "Madagascar",
    "MH": "Marshall Islands",
    "MK": "North Macedonia",
    "ML": "Mali",
    "MM": "Myanmar",
    "MN": "Mongolia",
    "MO": "Macao",
    "MP": "Northern Mariana Islands",
    "MQ": "Martinique",
    "MR": "Mauritania",
    "MS": "Montserrat",
    "MT": "Malta",
    "MU": "Mauritius",
    "MV": "Maldives",
    "MW": "Malawi",
    "MX": "Mexico",
    "MY": "Malaysia",
    "MZ": "Mozambique",
    "NA": "Namibia",
    "NC": "New Caledonia",
    "NE": "Niger",
    "NF": "Norfolk Island",
    "NG": "Nigeria",
    "NI": "Nicaragua",
    "NL": "Netherlands",
    "NO": "Norway",
    "NP": "Nepal",
    "NR": "Nauru",
    "NU": "Niue",
    "NZ": "New Zealand",
    "OM": "Oman",
    "PA": "Panama",
    "PE": "Peru",
    "PF": "French Polynesia",
    "PG": "Papua New Guinea",
    "PH": "Philippines",
    "PK": "Pakistan",
    "PL": "Poland",
    "PM": "Saint Pierre and Miquelon",
    "PN": "Pitcairn",
    "PR": "Puerto Rico",
    "PS": "Palestine",
    "PT": "Portugal",
    "PW": "Palau",
    "PY": "Paraguay",
    "QA": "Qatar",
    "RE": "Réunion",
    "RO": "Romania",
    "RS": "Serbia",
    "RU": "Russian Federation",
    "RW": "Rwanda",
    "SA": "Saudi Arabia",
    "SB": "Solomon Islands",
    "SC": "Seychelles",
    "SD": "Sudan",
    "SE": "Sweden",
    "SG": "Singapore",
    "SH": "Saint Helena",
    "SI": "Slovenia",
    "SJ": "Svalbard and Jan Mayen",
    "SK": "Slovakia",
    "SL": "Sierra Leone",
    "SM": "San Marino",
    "SN": "Senegal",
    "SO": "Somalia",
    "SR": "Suriname",
    "SS": "South Sudan",
    "ST": "Sao Tome and Principe",
    "SV": "El Salvador",
    "SX": "Sint Maarten",
    "SY": "Syria",
    "SZ": "Eswatini",
    "TC": "Turks and Caicos Islands",
    "TD": "Chad",
    "TF": "French Southern Territories",
    "TG": "Togo",
    "TH": "Thailand",
    "TJ": "Tajikistan",
    "TK": "Tokelau",
    "TL": "Timor-Leste",
    "TM": "Turkmenistan",
    "TN": "Tunisia",
    "TO": "Tonga",
    "TR": "Turkey",
    "TT": "Trinidad and Tobago",
    "TV": "Tuvalu",
    "TW": "Taiwan",
    "TZ": "Tanzania",
    "UA": "Ukraine",
    "UG": "Uganda",
    "UM": "United States Minor Outlying Islands",
    "US": "United States",
    "UY": "Uruguay",
    "UZ": "Uzbekistan",
    "VA": "Vatican City",
    "VC": "Saint Vincent and the Grenadines",
    "VE": "Venezuela",
    "VG": "Virgin Islands, British",
    "VI": "Virgin Islands, U.S.",
    "VN": "Vietnam",
    "VU": "Vanuatu",
    "WF": "Wallis and Futuna",
    "WS": "Samoa",
    "YE": "Yemen",
    "YT": "Mayotte",
    "ZA": "South Africa",
    "ZM": "Zambia",
    "ZW": "Zimbabwe",
}


def normalize_country(country: str, country_code: str) -> tuple[str, str]:
    """Clean and normalize country name and two-letter country code."""
    code = (country_code or "").strip().upper()
    name = (country or "").strip()

    canonical = COUNTRY_NAMES.get(code)
    if canonical:
        if not name or name.upper() == code:
            name = canonical
        elif canonical.lower().endswith(name.lower()):
            # Handle corrupted names where the first letters were sliced off
            # (e.g. "TH" sliced from "Thailand" -> "ailand", "IN" from "India" -> "dia")
            name = canonical
        elif name.lower() in ("vietnam", "viet nam"):
            name = "Vietnam"
        elif name.lower() in ("korea", "south korea", "korea republic"):
            name = "Korea Republic"
        elif name.lower() in ("russia", "russian federation"):
            name = "Russian Federation"
    elif not code and name:
        for c_code, c_name in COUNTRY_NAMES.items():
            if c_name.lower() == name.lower():
                code = c_code
                name = c_name
                break

    return name, code


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
    real_ping_ms: Optional[int] = None      # Measured latency from client PC in ms (-1 if unreachable)

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
        if self.country or self.country_code:
            self.country, self.country_code = normalize_country(self.country, self.country_code)

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
    def real_ping_text(self) -> str:
        if self.real_ping_ms is None:
            return ""
        if self.real_ping_ms < 0:
            return "Unreachable"
        return f"{self.real_ping_ms} ms"

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
            "real_ping_ms": self.real_ping_ms,
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

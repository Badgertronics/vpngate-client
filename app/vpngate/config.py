import os
import sys

APP_NAME = "VPN Gate Client"
APP_ID = "com.vpngate.client"
VERSION = "1.0.3"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STATE_DIR = os.path.join(BASE_DIR, "state")
LOG_DIR = os.path.join(BASE_DIR, "logs")
CONFIG_FILE = os.path.join(STATE_DIR, "settings.json")

# ---------------------------------------------------------------------------
# VPN Gate mirror sites.  The list is refreshed from the daily mirror mail /
# https://www.vpngate.net/en/sites.aspx, but these are the current daily ones.
# ---------------------------------------------------------------------------
DEFAULT_MIRRORS = [
    "http://150.40.105.11:4917",
    "http://150.40.105.5:32536",
    "http://150.40.105.6:11803",
    "http://178.32.222.148:18609",
    "http://150.40.105.17:50406",
    "https://www.vpngate.net",
]

# Credentials for every public VPN Gate relay server.
VPN_USERNAME = "vpn"
VPN_PASSWORD = "vpn"
# Pre-shared key for L2TP/IPsec
VPN_PSK = "vpn"
# Virtual Hub name used by the SoftEther (SSL-VPN) service on VPN Gate servers
SOFTETHER_HUB = "VPNGATE"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Seconds to wait for a tunnel to come up before giving up.
CONNECT_TIMEOUT = 45
# Seconds to wait for a SoftEther session to be established.
SOFTETHER_TIMEOUT = 60

# ---------------------------------------------------------------------------
# Online data published by this project's own repository.
#
# A scheduled GitHub Action (.github/workflows/refresh-mirrors.yml) refreshes
# data/mirrors.json (the daily mirror list scraped from
# https://www.vpngate.net/en/sites.aspx) and data/servers.csv (a verbatim copy
# of https://www.vpngate.net/api/iphone/) every day.  When the mirrors
# themselves are unreachable the client reads those two files from here.
# ---------------------------------------------------------------------------
GITHUB_REPO = "Badgertronics/vpngate-client"
REPO_BRANCH = "main"
GITHUB_URL = f"https://github.com/{GITHUB_REPO}"

_REPO_RAW_PREFIXES = [
    # GitHub raw first, then the jsDelivr CDN copies of the same files (the
    # CDN is usually reachable even when raw.githubusercontent.com is not).
    f"https://raw.githubusercontent.com/{GITHUB_REPO}/{REPO_BRANCH}",
    f"https://cdn.jsdelivr.net/gh/{GITHUB_REPO}@{REPO_BRANCH}",
    f"https://fastly.jsdelivr.net/gh/{GITHUB_REPO}@{REPO_BRANCH}",
    f"https://gcore.jsdelivr.net/gh/{GITHUB_REPO}@{REPO_BRANCH}",
    f"https://testingcf.jsdelivr.net/gh/{GITHUB_REPO}@{REPO_BRANCH}",
]

REPO_MIRROR_LIST_PATH = "data/mirrors.json"
REPO_CSV_PATH = "data/servers.csv"

REPO_MIRROR_LIST_URLS = [f"{prefix}/{REPO_MIRROR_LIST_PATH}"
                         for prefix in _REPO_RAW_PREFIXES]
REPO_CSV_URLS = [f"{prefix}/{REPO_CSV_PATH}" for prefix in _REPO_RAW_PREFIXES]

# ---------------------------------------------------------------------------
# "Buy me a coffee" — fill in your own wallet addresses here; they are shown
# in the About dialog and in the Settings → Support page (with copy buttons).
# ---------------------------------------------------------------------------
DEVELOPER_NAME = "Mr. Honey Badger (Badgertronics)"

DONATE_BITCOIN = "bc1q3l2gxktlc3r9f6z325d920kqhv8kwctqdyvxmh"
DONATE_ETHEREUM = "0x9C2Cde3575769e104E93bC934abbfE2D883e8dCa"
DONATE_BEP20 = "0x9C2Cde3575769e104E93bC934abbfE2D883e8dCa"

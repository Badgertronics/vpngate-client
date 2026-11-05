"""Refresh the relay data committed to this repository.

Run every day by the scheduled GitHub Action (``refresh-mirrors.yml``),
and perfectly usable locally too::

    python scripts/update_data.py

It produces two artifacts under ``data/``:

``data/mirrors.json``
    The daily mirror-site list scraped from
    https://www.vpngate.net/en/sites.aspx — the same list the "Daily
    Mirror URLs" mail subscription sends out.  The client reads this file
    (via GitHub raw / jsDelivr) before falling back to its compiled-in
    mirrors, so the app keeps working even when every mirror it knew of
    has rotated away.

``data/servers.csv``
    A verbatim copy of the official relay CSV feed
    https://www.vpngate.net/api/iphone/ (including the per-row OpenVPN
    configuration blob).  The client can rebuild its whole server table
    from this file alone when every mirror is unreachable.

Files are only rewritten when their content actually changed, so a quiet
day produces no commit.
"""
from __future__ import annotations

import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

SITES_URL = "https://www.vpngate.net/en/sites.aspx"
API_URL = "https://www.vpngate.net/api/iphone/"
PRIMARY_URL = "https://www.vpngate.net"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Mirror links look like  href='http://150.40.105.11:4917/en/'  or
# href='http://ns374305.ip-178-32-222.eu:18609/en/'  — always host:port.
_MIRROR_RE = re.compile(
    r"https?://[A-Za-z0-9][A-Za-z0-9.\-]*:\d+(?:/en/?)?", re.I)
_MIN_CSV_BYTES = 40_000          # the real feed is far larger than this


def _get(url: str, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,text/csv,*/*;q=0.9",
        "Accept-Language": "en-US,en;q=0.9",
    })
    # The mirror/primary certificates are famously self-signed or stale;
    # the payload is validated by content below, so transport pinning of
    # the host certificate is not what keeps this script safe.
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(request, timeout=timeout, context=context) \
            as response:
        return response.read()


def fetch_mirror_list() -> list:
    """Scrape the current mirror base URLs off the mirror-sites page."""
    page = _get(SITES_URL).decode("utf-8", "replace")
    mirrors: list = []
    seen: set = set()
    for match in _MIRROR_RE.finditer(page):
        base = match.group(0)
        # keep the bare "http://host:port" as the mirror root
        base = re.sub(r"/en/?$", "", base, flags=re.I)
        base = base.rstrip("/")
        if base not in seen:
            seen.add(base)
            mirrors.append(base)
    return mirrors


def fetch_servers_csv() -> str:
    """Download the official relay CSV feed verbatim."""
    body = _get(API_URL, timeout=120).decode("utf-8", "replace")
    if len(body) < _MIN_CSV_BYTES or "#HostName" not in body:
        raise RuntimeError("the CSV feed answered but looks truncated")
    return body


def _write_if_changed(path: Path, text: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def fetch_latest_release_info() -> dict | None:
    """Fetch metadata for the latest GitHub release."""
    urls = [
        "https://api.github.com/repos/Badgertronics/vpngate-client/releases/latest",
        "https://gh-proxy.com/https://api.github.com/repos/Badgertronics/vpngate-client/releases/latest",
        "https://ghproxy.net/https://api.github.com/repos/Badgertronics/vpngate-client/releases/latest",
        "https://ghproxy.cc/https://api.github.com/repos/Badgertronics/vpngate-client/releases/latest",
    ]
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/vnd.github.v3+json,application/json,*/*",
            })
            with urllib.request.urlopen(req, timeout=15, context=context) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
                if isinstance(data, dict) and data.get("tag_name"):
                    return {
                        "tag_name": data.get("tag_name"),
                        "name": data.get("name") or data.get("tag_name"),
                        "body": data.get("body", ""),
                        "html_url": data.get("html_url", ""),
                        "published_at": data.get("published_at", ""),
                        "assets": [
                            {
                                "name": a.get("name"),
                                "browser_download_url": a.get("browser_download_url"),
                                "size": a.get("size", 0),
                            }
                            for a in data.get("assets", [])
                        ]
                    }
        except Exception:
            continue
    return None


def main() -> int:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    updated_any = False
    fetched_any = False

    # ---- mirror list -----------------------------------------------------
    try:
        mirrors = fetch_mirror_list()
    except Exception as exc:                      # noqa: BLE001
        print(f"!! mirror list fetch failed: {exc}", file=sys.stderr)
        print("   (keeping the previous data/mirrors.json)", file=sys.stderr)
    else:
        fetched_any = True
        mirrors_file = DATA_DIR / "mirrors.json"
        existing_mirrors = []
        if mirrors_file.exists():
            try:
                existing_data = json.loads(mirrors_file.read_text(encoding="utf-8"))
                existing_mirrors = existing_data.get("mirrors", [])
            except Exception:
                existing_mirrors = []

        if mirrors != existing_mirrors or not mirrors_file.exists():
            payload = {
                "updated": now,
                "primary": PRIMARY_URL,
                "mirrors": mirrors,
            }
            changed = _write_if_changed(
                mirrors_file,
                json.dumps(payload, indent=2) + "\n")
        else:
            changed = False
        print(f"mirrors.json: {len(mirrors)} mirror(s), "
              f"{'written' if changed else 'unchanged'}")
        updated_any = updated_any or changed

    # ---- full CSV ---------------------------------------------------------
    try:
        csv_text = fetch_servers_csv()
    except Exception as exc:                      # noqa: BLE001
        print(f"!! servers.csv fetch failed: {exc}", file=sys.stderr)
        print("   (keeping the previous data/servers.csv)", file=sys.stderr)
    else:
        fetched_any = True
        changed = _write_if_changed(DATA_DIR / "servers.csv", csv_text)
        print(f"servers.csv: {len(csv_text):,} bytes, "
              f"{'written' if changed else 'unchanged'}")
        updated_any = updated_any or changed

    # ---- version.json -----------------------------------------------------
    try:
        ver_info = fetch_latest_release_info()
        if ver_info and ver_info.get("tag_name"):
            changed = _write_if_changed(
                DATA_DIR / "version.json",
                json.dumps(ver_info, indent=2) + "\n"
            )
            print(f"version.json: {ver_info.get('tag_name')}, "
                  f"{'written' if changed else 'unchanged'}")
            updated_any = updated_any or changed
    except Exception as exc:
        print(f"!! version.json fetch note: {exc}", file=sys.stderr)

    if not fetched_any:
        print("!! nothing could be fetched — leaving the old data alone",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

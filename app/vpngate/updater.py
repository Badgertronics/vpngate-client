"""GitHub release update checker and installer for VPN Gate Client."""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from typing import Callable, Optional, Tuple

from vpngate.config import (GITHUB_REPO, GITHUB_URL, RELEASE_API_MIRRORS,
                            RELEASE_ASSET_MIRROR_PREFIXES, REPO_VERSION_URLS,
                            VERSION)
from vpngate.netutil import (download_file_resilient, download_json_resilient,
                            download_json_resilient_with_error)

GITHUB_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


def parse_version(v: str) -> Tuple[int, ...]:
    """Parse version string like 'v1.0.19' or '1.0.18' into an integer tuple."""
    nums = re.findall(r"\d+", str(v))
    return tuple(int(x) for x in nums) if nums else (0,)


def is_newer(remote_tag: str, local_ver: str) -> bool:
    """Return True if remote_tag is strictly newer than local_ver."""
    return parse_version(remote_tag) > parse_version(local_ver)


def find_matching_asset(assets: list) -> Optional[dict]:
    """Find the best matching binary asset for the current OS and architecture."""
    system = sys.platform.lower()
    machine = platform.machine().lower()

    if system == "win32":
        # Look for Windows zip (e.g. vpngate-client-v1.0.19-windows-x86_64.zip)
        for a in assets:
            name = a.get("name", "").lower()
            if name.endswith(".zip") and ("windows" in name or "win" in name):
                return {
                    "name": a["name"],
                    "url": a.get("browser_download_url"),
                    "size": a.get("size", 0),
                }
        return None

    if "linux" in system:
        is_arm = "arm" in machine or "aarch64" in machine
        wanted_deb_arch = "arm64" if is_arm else "amd64"
        wanted_tar_arch = "arm64" if is_arm else "x86_64"

        # 1. Prefer .deb package on Debian/Ubuntu
        for a in assets:
            name = a.get("name", "").lower()
            if name.endswith(".deb") and wanted_deb_arch in name:
                return {
                    "name": a["name"],
                    "url": a.get("browser_download_url"),
                    "size": a.get("size", 0),
                }

        # 2. Fall back to .tar.gz bundle
        for a in assets:
            name = a.get("name", "").lower()
            if name.endswith(".tar.gz") and wanted_tar_arch in name:
                return {
                    "name": a["name"],
                    "url": a.get("browser_download_url"),
                    "size": a.get("size", 0),
                }

    return None


def check_for_updates() -> Tuple[Optional[dict], Optional[str]]:
    """Check for a newer release across GitHub API and mirror proxies/CDNs.

    Returns:
        (info_dict, None) if a newer version is available.
        (None, None) if check succeeded and local version is already up to date.
        (None, error_str) if all endpoints failed.
    """
    candidate_urls = list(RELEASE_API_MIRRORS) + list(REPO_VERSION_URLS)
    last_err = "No update sources could be reached"
    valid_data = None

    for url in candidate_urls:
        try:
            data, err = download_json_resilient_with_error(url, timeout=10)
            if err:
                last_err = err
            if data and isinstance(data, dict):
                tag = (data.get("tag_name") or data.get("version") or "").strip()
                if tag:
                    valid_data = data
                    break
        except Exception as exc:
            last_err = str(exc)

    if not valid_data:
        return None, last_err

    tag = (valid_data.get("tag_name") or valid_data.get("version") or "").strip()
    if not is_newer(tag, VERSION):
        # Successfully checked: local version is already the latest!
        return None, None

    assets = valid_data.get("assets", [])
    matching_asset = find_matching_asset(assets)

    return {
        "tag_name": tag,
        "name": valid_data.get("name") or tag,
        "body": valid_data.get("body") or "",
        "html_url": valid_data.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases/latest",
        "asset": matching_asset,
    }, None


def download_update_asset(asset_url: str, filename: str,
                          expected_size: int = 0,
                          log_cb: Optional[Callable[[str], None]] = None) -> Optional[str]:
    """Download update package into a temporary file with live progress logging and mirror fallbacks."""
    dest_path = os.path.join(tempfile.gettempdir(), filename)

    candidate_urls: List[Tuple[str, str]] = []
    # 1. Direct download URL
    candidate_urls.append((asset_url, "GitHub direct"))

    # 2. Add mirror proxies if it's a GitHub URL
    if "github.com" in asset_url:
        for prefix in RELEASE_ASSET_MIRROR_PREFIXES:
            if not prefix:
                continue
            mirror_url = f"{prefix.rstrip('/')}/{asset_url}"
            mirror_label = prefix.split("//")[-1].strip("/")
            candidate_urls.append((mirror_url, f"mirror {mirror_label}"))

    total_candidates = len(candidate_urls)
    for idx, (url, label) in enumerate(candidate_urls, 1):
        if log_cb:
            if idx == 1:
                log_cb(f"[update] Downloading {filename} via {label} ...")
            else:
                log_cb(f"[update] Retrying download via {label} ({idx}/{total_candidates}) ...")

        try:
            if os.path.exists(dest_path):
                os.remove(dest_path)
        except Exception:
            pass

        ok = download_file_resilient(
            url, dest_path, timeout=180, expected_size=expected_size,
            log_cb=log_cb, prefix="[update]"
        )
        if ok and os.path.isfile(dest_path) and os.path.getsize(dest_path) > 0:
            return dest_path

    if log_cb:
        log_cb(f"[update] ✗ Failed to download {filename} across all {total_candidates} sources")
    return None


def apply_windows_update(zip_path: str, app_dir: str, current_pid: int) -> bool:
    """Extract update zip and launch detached updater script to replace files and restart."""
    extract_dir = os.path.join(tempfile.gettempdir(), "vpngate_update_extract")
    try:
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir, ignore_errors=True)
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)
    except Exception as exc:
        print(f"[updater] Failed to extract zip: {exc}", file=sys.stderr)
        return False

    # The archive contains a root folder 'vpngate-client'
    src_folder = os.path.join(extract_dir, "vpngate-client")
    if not os.path.isdir(src_folder):
        src_folder = extract_dir

    script_path = os.path.join(tempfile.gettempdir(), "vpngate_update_runner.ps1")
    exe_target = os.path.join(app_dir, "vpngate-gui.exe")

    ps_content = f"""# VPN Gate Client Self-Updater
$ErrorActionPreference = 'SilentlyContinue'
$procId = {current_pid}
$appDir = "{app_dir}"
$srcDir = "{src_folder}"
$exeTarget = "{exe_target}"

try {{
    Wait-Process -Id $procId -Timeout 15
}} catch {{}}
Start-Sleep -Seconds 1

Copy-Item -Path "$srcDir\\*" -Destination "$appDir" -Recurse -Force
Start-Process -FilePath "$exeTarget"

Start-Sleep -Seconds 2
Remove-Item -Path "{extract_dir}" -Recurse -Force
Remove-Item -Path "{zip_path}" -Force
Remove-Item -Path $MyInvocation.MyCommand.Path -Force
"""
    try:
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(ps_content)
    except Exception as exc:
        print(f"[updater] Failed to write updater script: {exc}", file=sys.stderr)
        return False

    creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | \
                     getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

    try:
        subprocess.Popen(
            ["powershell.exe", "-ExecutionPolicy", "Bypass", "-NoProfile", "-WindowStyle", "Hidden", "-File", script_path],
            creationflags=creation_flags,
            close_fds=True
        )
        return True
    except Exception as exc:
        print(f"[updater] Failed to launch updater script: {exc}", file=sys.stderr)
        return False


def apply_linux_update(deb_path: str) -> Tuple[bool, str]:
    """Install .deb update package on Linux via pkexec dpkg -i."""
    try:
        cmd = ["pkexec", "dpkg", "-i", deb_path]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if res.returncode == 0:
            return True, "Package installed successfully."
        return False, f"dpkg failed (code {res.returncode}): {res.stderr.strip() or res.stdout.strip()}"
    except Exception as exc:
        return False, str(exc)

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

from vpngate.config import VERSION
from vpngate.netutil import download_file_resilient, download_json_resilient

GITHUB_API_LATEST = "https://api.github.com/repos/Badgertronics/vpngate-client/releases/latest"


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


def check_for_updates() -> Optional[dict]:
    """Check GitHub for a newer release. Returns release metadata if update is available."""
    data = download_json_resilient(GITHUB_API_LATEST, timeout=15)
    if not data or not isinstance(data, dict):
        return None

    tag = data.get("tag_name", "").strip()
    if not tag or not is_newer(tag, VERSION):
        return None

    assets = data.get("assets", [])
    matching_asset = find_matching_asset(assets)

    return {
        "tag_name": tag,
        "name": data.get("name") or tag,
        "body": data.get("body") or "",
        "html_url": data.get("html_url") or "https://github.com/Badgertronics/vpngate-client/releases/latest",
        "asset": matching_asset,
    }


def download_update_asset(asset_url: str, filename: str,
                          expected_size: int = 0,
                          log_cb: Optional[Callable[[str], None]] = None) -> Optional[str]:
    """Download update package into a temporary file with live progress logging."""
    dest_path = os.path.join(tempfile.gettempdir(), filename)
    if log_cb:
        log_cb(f"[update] Downloading {filename} ...")
    ok = download_file_resilient(asset_url, dest_path, timeout=180, expected_size=expected_size,
                                 log_cb=log_cb, prefix="[update]")
    if ok and os.path.isfile(dest_path) and os.path.getsize(dest_path) > 0:
        return dest_path
    if log_cb:
        log_cb(f"[update] ✗ Failed to download {filename}")
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

#!/usr/bin/env python3
"""Build and package VPN Gate Client for release.

Usage:
    python scripts/build.py [--deb] [--output-dir dist_release]
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app" if (ROOT / "app").exists() else ROOT

# Extract version from app/vpngate/config.py
sys.path.insert(0, str(APP_DIR))
try:
    from vpngate.config import APP_NAME, VERSION
except ImportError:
    APP_NAME = "VPN Gate Client"
    VERSION = "1.0.0"


def get_platform_info() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()

    if "linux" in system:
        os_name = "linux"
    elif "win" in system:
        os_name = "windows"
    elif "darwin" in system:
        os_name = "macos"
    else:
        os_name = system

    if machine in ("x86_64", "amd64"):
        arch = "x86_64"
        deb_arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
        deb_arch = "arm64"
    elif machine in ("i386", "i686", "x86"):
        arch = "x86"
        deb_arch = "i386"
    else:
        arch = machine
        deb_arch = machine

    return os_name, arch, deb_arch


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print(f"==> Running: {' '.join(str(c) for c in cmd)}")
    res = subprocess.run(cmd, cwd=cwd)
    if res.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {res.returncode}")


def build_pyinstaller(build_dir: Path, dist_dir: Path) -> Path:
    pyinstaller = shutil.which("pyinstaller")
    if not pyinstaller:
        user_bin = Path.home() / ".local" / "bin" / "pyinstaller"
        if user_bin.is_file():
            pyinstaller = str(user_bin)
        else:
            raise RuntimeError("pyinstaller executable not found in PATH")

    bundle_dir = dist_dir / "vpngate-bundle"
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)

    hooks_dir = ROOT / "scripts" / "hooks"
    icon_path = APP_DIR / "icons" / "vpngate.ico"
    icon_args = ["--icon", str(icon_path)] if icon_path.exists() else []

    print("==> Building GUI executable with PyInstaller...")
    gui_cmd = [
        pyinstaller,
        "--noconfirm",
        "--onedir",
        "--clean",
        "--name", "vpngate-gui",
        *icon_args,
        "--distpath", str(bundle_dir),
        "--workpath", str(build_dir / "build-gui"),
        "--additional-hooks-dir", str(hooks_dir),
        str(APP_DIR / "vpngate_gui.py"),
    ]
    run(gui_cmd, cwd=ROOT)

    print("==> Building root worker executable with PyInstaller...")
    worker_cmd = [
        pyinstaller,
        "--noconfirm",
        "--onedir",
        "--clean",
        "--name", "vpngate-worker",
        *icon_args,
        "--distpath", str(bundle_dir),
        "--workpath", str(build_dir / "build-worker"),
        str(APP_DIR / "worker_main.py"),
    ]
    run(worker_cmd, cwd=ROOT)

    # In onedir mode, pyinstaller creates bundle_dir/vpngate-gui/ and bundle_dir/vpngate-worker/
    # We assemble a unified distribution directory:
    out_dir = dist_dir / "vpngate-client"
    if out_dir.exists():
        shutil.rmtree(out_dir)

    # Move vpngate-gui contents as the main directory
    gui_dist = bundle_dir / "vpngate-gui"
    shutil.move(str(gui_dist), str(out_dir))

    # Copy vpngate-worker executable and any unique worker binaries into out_dir
    worker_dist = bundle_dir / "vpngate-worker"
    worker_exe_name = "vpngate-worker.exe" if sys.platform == "win32" else "vpngate-worker"
    worker_binary = worker_dist / worker_exe_name
    if worker_binary.exists():
        shutil.copy2(str(worker_binary), str(out_dir / worker_exe_name))
        try:
            os.chmod(str(out_dir / worker_exe_name), 0o755)
        except OSError:
            pass
    # Ensure gdbus.exe is bundled on Windows to satisfy GIO win32 session bus
    if sys.platform == "win32":
        gdbus_candidates = [
            shutil.which("gdbus"),
            shutil.which("gdbus.exe"),
            Path("C:/Program Files/msys64/ucrt64/bin/gdbus.exe"),
            Path("C:/msys64/ucrt64/bin/gdbus.exe"),
            Path("C:/msys64/mingw64/bin/gdbus.exe"),
        ]
        gdbus_bin = next((Path(p) for p in gdbus_candidates if p and Path(p).is_file()), None)
        if gdbus_bin:
            shutil.copy2(str(gdbus_bin), str(out_dir / "gdbus.exe"))
            if (out_dir / "_internal").exists():
                shutil.copy2(str(gdbus_bin), str(out_dir / "_internal" / "gdbus.exe"))
            print(f"==> Bundled win32 session dbus helper: {gdbus_bin}")

    # Copy icons, desktop file, install.sh, data, README
    icons_src = APP_DIR / "icons"
    if icons_src.exists():
        shutil.copytree(str(icons_src), str(out_dir / "icons"), dirs_exist_ok=True)
        if (out_dir / "_internal").exists():
            shutil.copytree(str(icons_src), str(out_dir / "_internal" / "icons"), dirs_exist_ok=True)

    data_src = ROOT / "data"
    if data_src.exists():
        shutil.copytree(str(data_src), str(out_dir / "data"), dirs_exist_ok=True)
        if (out_dir / "_internal").exists():
            shutil.copytree(str(data_src), str(out_dir / "_internal" / "data"), dirs_exist_ok=True)

    install_src = APP_DIR / "install.sh"
    if install_src.exists():
        shutil.copy2(str(install_src), str(out_dir / "install.sh"))
        os.chmod(str(out_dir / "install.sh"), 0o755)

    readme_src = ROOT / "README.md"
    if readme_src.exists():
        shutil.copy2(str(readme_src), str(out_dir / "README.md"))

    # Cleanup temporary bundle_dir
    shutil.rmtree(bundle_dir, ignore_errors=True)
    return out_dir


def package_tar_gz(source_dir: Path, output_file: Path) -> None:
    print(f"==> Creating tarball: {output_file.name}")
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output_file, "w:gz") as tar:
        tar.add(source_dir, arcname="vpngate-client")


def package_zip(source_dir: Path, output_file: Path) -> None:
    print(f"==> Creating zip archive: {output_file.name}")
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_file, "w", zipfile.ZIP_DEFLATED) as zipf:
        for file in source_dir.rglob("*"):
            if file.is_file():
                arcname = file.relative_to(source_dir.parent)
                zipf.write(file, arcname=str(arcname))


def package_deb(source_dir: Path, output_deb: Path, deb_arch: str) -> None:
    dpkg_deb = shutil.which("dpkg-deb")
    if not dpkg_deb:
        print("Warning: dpkg-deb not found, skipping .deb creation.")
        return

    print(f"==> Packaging Debian package: {output_deb.name}")
    deb_root = output_deb.parent / "deb_staging"
    if deb_root.exists():
        shutil.rmtree(deb_root)

    install_lib = deb_root / "usr" / "lib" / "vpngate-client"
    install_bin = deb_root / "usr" / "bin"
    install_apps = deb_root / "usr" / "share" / "applications"
    install_icons = deb_root / "usr" / "share" / "icons" / "hicolor" / "scalable" / "apps"
    debian_dir = deb_root / "DEBIAN"

    install_lib.mkdir(parents=True, exist_ok=True)
    install_bin.mkdir(parents=True, exist_ok=True)
    install_apps.mkdir(parents=True, exist_ok=True)
    install_icons.mkdir(parents=True, exist_ok=True)
    debian_dir.mkdir(parents=True, exist_ok=True)

    # Copy files to /usr/lib/vpngate-client
    shutil.copytree(str(source_dir), str(install_lib), dirs_exist_ok=True)

    # Symlink executables into /usr/bin
    gui_target = Path("/usr/lib/vpngate-client/vpngate-gui")
    worker_target = Path("/usr/lib/vpngate-client/vpngate-worker")
    (install_bin / "vpngate-gui").symlink_to(gui_target)
    (install_bin / "vpngate-worker").symlink_to(worker_target)

    # Sudoers rule to allow running worker without password prompt
    install_sudoers = deb_root / "etc" / "sudoers.d"
    install_sudoers.mkdir(parents=True, exist_ok=True)
    sudoers_file = install_sudoers / "vpngate-client"
    sudoers_file.write_text(
        "# Allow running the VPN Gate worker without password prompt\n"
        "ALL ALL=(ALL) NOPASSWD: /usr/lib/vpngate-client/vpngate-worker, /usr/bin/vpngate-worker, /usr/bin/vpngate-gui\n",
        encoding="utf-8"
    )
    sudoers_file.chmod(0o440)

    # Copy desktop entry
    desktop_file = install_apps / "com.vpngate.client.desktop"
    desktop_file.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=VPN Gate Client\n"
        "GenericName=VPN Client\n"
        "Comment=Connect to the public free VPN servers of VPN Gate\n"
        "Exec=/usr/bin/vpngate-gui\n"
        "Icon=com.vpngate.client\n"
        "Terminal=false\n"
        "Categories=Network;Security;\n"
        "Keywords=vpn;vpngate;openvpn;sstp;l2tp;softether;\n"
        "StartupNotify=true\n"
        "StartupWMClass=com.vpngate.client\n"
        "URL=https://github.com/Badgertronics/vpngate-client\n",
        encoding="utf-8"
    )

    # AppStream metadata for App Center
    install_metainfo = deb_root / "usr" / "share" / "metainfo"
    install_metainfo.mkdir(parents=True, exist_ok=True)
    metainfo_file = install_metainfo / "com.vpngate.client.metainfo.xml"
    metainfo_file.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<component type="desktop-application">\n'
        '  <id>com.vpngate.client</id>\n'
        '  <metadata_license>CC0-1.0</metadata_license>\n'
        '  <project_license>MIT</project_license>\n'
        '  <name>VPN Gate Client</name>\n'
        '  <summary>Free graphical VPN Gate client for Linux desktops</summary>\n'
        '  <description>\n'
        '    <p>Connect to the public free VPN servers of the University of Tsukuba VPN Gate project.</p>\n'
        '    <p>Supports SSL-VPN (SoftEther), L2TP/IPsec, MS-SSTP, and OpenVPN.</p>\n'
        '  </description>\n'
        '  <launchable type="desktop-id">com.vpngate.client.desktop</launchable>\n'
        '  <url type="homepage">https://github.com/Badgertronics/vpngate-client</url>\n'
        '  <url type="bugtracker">https://github.com/Badgertronics/vpngate-client/issues</url>\n'
        '  <url type="vcs-browser">https://github.com/Badgertronics/vpngate-client</url>\n'
        '  <url type="help">https://github.com/Badgertronics/vpngate-client#readme</url>\n'
        '  <developer id="com.vpngate.client">\n'
        '    <name>Badgertronics</name>\n'
        '  </developer>\n'
        '  <developer_name>Badgertronics</developer_name>\n'
        '</component>\n',
        encoding="utf-8"
    )

    # Copy icons
    icon_src_dir = source_dir / "icons" / "hicolor" / "scalable" / "apps"
    if icon_src_dir.exists():
        for svg in icon_src_dir.glob("*.svg"):
            shutil.copy2(str(svg), str(install_icons / svg.name))

    # Control file
    control_file = debian_dir / "control"
    control_file.write_text(
        "Package: vpngate-client\n"
        f"Version: {VERSION}\n"
        "Section: net\n"
        "Priority: optional\n"
        f"Architecture: {deb_arch}\n"
        "Maintainer: Mr. Honey Badger (Badgertronics)\n"
        "Homepage: https://github.com/Badgertronics/vpngate-client\n"
        "Depends: openvpn, strongswan, xl2tpd, sstp-client, python3-netfilterqueue\n"
        "Recommends: softether-vpnclient\n"
        "Description: Graphical VPN Gate client for Linux desktops\n"
        " Connect to the public free VPN servers of VPN Gate.\n"
        " Supports SSL-VPN (SoftEther), L2TP/IPsec, MS-SSTP, and OpenVPN.\n",
        encoding="utf-8"
    )

    # postinst script to ensure proper sudoers file permission and remove legacy duplicate desktop file
    postinst_file = debian_dir / "postinst"
    postinst_file.write_text(
        "#!/bin/sh\n"
        "set -e\n"
        "if [ -f /etc/sudoers.d/vpngate-client ]; then\n"
        "    chown root:root /etc/sudoers.d/vpngate-client\n"
        "    chmod 0440 /etc/sudoers.d/vpngate-client\n"
        "fi\n"
        "rm -f /usr/share/applications/vpngate-gui.desktop\n"
        "if which update-desktop-database >/dev/null 2>&1; then\n"
        "    update-desktop-database /usr/share/applications || true\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8"
    )
    postinst_file.chmod(0o755)

    run([dpkg_deb, "--build", "--root-owner-group", str(deb_root), str(output_deb)])
    shutil.rmtree(deb_root, ignore_errors=True)
    print(f"==> Successfully generated {output_deb}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and package VPN Gate Client")
    parser.add_argument("--deb", action="store_true", help="Build .deb package on Linux")
    parser.add_argument("--output-dir", default="dist_release", help="Release directory")
    args = parser.parse_args()

    os_name, arch, deb_arch = get_platform_info()
    out_dir = ROOT / args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    temp_build_dir = ROOT / "build_tmp"
    temp_build_dir.mkdir(parents=True, exist_ok=True)

    print(f"==> Packaging {APP_NAME} v{VERSION} for {os_name}-{arch}")
    packaged_dir = build_pyinstaller(temp_build_dir, out_dir)

    if os_name == "linux":
        tar_name = f"vpngate-client-v{VERSION}-linux-{arch}.tar.gz"
        package_tar_gz(packaged_dir, out_dir / tar_name)
        if args.deb:
            deb_name = f"vpngate-client_{VERSION}_{deb_arch}.deb"
            package_deb(packaged_dir, out_dir / deb_name, deb_arch)
    elif os_name == "windows":
        zip_name = f"vpngate-client-v{VERSION}-windows-{arch}.zip"
        package_zip(packaged_dir, out_dir / zip_name)
    else:
        tar_name = f"vpngate-client-v{VERSION}-{os_name}-{arch}.tar.gz"
        package_tar_gz(packaged_dir, out_dir / tar_name)

    shutil.rmtree(temp_build_dir, ignore_errors=True)
    print(f"==> All release artifacts created in: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

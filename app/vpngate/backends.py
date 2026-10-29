"""Protocol backends.

Every backend implements :meth:`Backend.connect` and
:meth:`Backend.disconnect`.  ``connect`` returns a plain dictionary with the
connection details, ``disconnect`` cleans everything up again.

All backends run inside the privileged worker process (root).
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import Callable, Dict, List, Optional

from . import netutil
from .config import (CONNECT_TIMEOUT, SOFTETHER_HUB, SOFTETHER_TIMEOUT,
                     STATE_DIR, VPN_PASSWORD, VPN_PSK, VPN_USERNAME)
from .models import Protocol, VpnServer

Emit = Callable[..., None]


class BackendError(Exception):
    """Raised when a connection cannot be established."""


def _log_emit(emit: Emit, text: str) -> None:
    emit("log", text=text)


def _since(start: float) -> str:
    """Seconds since *start* (monotonic), formatted for a log line."""
    return f"{time.monotonic() - start:.1f} s"


# ---------------------------------------------------------------------------
# Backend base
# ---------------------------------------------------------------------------

class Backend:
    """Common interface of all protocol backends."""

    name = "backend"
    supports: tuple = ()

    def available(self) -> bool:
        raise NotImplementedError

    def connect(self, server: VpnServer, protocol: Protocol, emit: Emit,
                workdir: str) -> dict:
        raise NotImplementedError

    def disconnect(self, info: dict, emit: Emit) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# OpenVPN
# ---------------------------------------------------------------------------

class OpenVpnBackend:
    name = "OpenVPN"
    supports = (Protocol.OPENVPN,)

    def available(self) -> bool:
        if sys.platform == "win32":
            return bool(self._openvpn_bin())
        return bool(netutil.which("openvpn"))

    @staticmethod
    def _is_connect(bin_path: str) -> bool:
        return bool(bin_path and bin_path.lower().endswith("openvpnconnect.exe"))

    @staticmethod
    def _openvpn_bin() -> str:
        if sys.platform == "win32":
            found = shutil.which("openvpn")
            if found:
                return found
            for p in (r"C:\Program Files\OpenVPN\bin\openvpn.exe",
                      r"C:\Program Files (x86)\OpenVPN\bin\openvpn.exe",
                      r"C:\Program Files\OpenVPN Connect\OpenVPNConnect.exe",
                      r"C:\Program Files (x86)\OpenVPN Connect\OpenVPNConnect.exe"):
                if os.path.exists(p):
                    return p
            return ""
        return netutil.which("openvpn") or ""

    # -- configuration ----------------------------------------------------
    @staticmethod
    def decode_config(server: VpnServer) -> str:
        if not server.openvpn_config_b64:
            return ""
        try:
            return base64.b64decode(server.openvpn_config_b64).decode("utf-8", "replace")
        except Exception:                          # noqa: BLE001
            return ""

    def download_config(self, server: VpnServer, use_udp: bool,
                        emit: Emit) -> str:
        """Download the .ovpn profile published for *server*."""
        if not server.openvpn_download_base:
            raise BackendError(
                "This server does not publish an OpenVPN configuration.")
        port = server.openvpn_udp if use_udp else server.openvpn_tcp
        if not port:
            raise BackendError("This server does not offer that OpenVPN port.")
        flag = "0" if use_udp else "1"
        kind = "udp" if use_udp else "tcp"
        url = (f"{server.openvpn_download_base}&tcp={flag}&host={server.ip}"
               f"&port={port}&/vpngate_{server.ip}_{kind}_{port}.ovpn")
        _log_emit(emit, f"Downloading the OpenVPN profile from {server.hostname} ...")
        try:
            from .fetcher import http_get
            data = http_get(url, timeout=45).decode("utf-8", "replace")
        except Exception as exc:                  # noqa: BLE001
            raise BackendError(
                f"Could not download the OpenVPN configuration: {exc}") from exc
        if "remote" not in data or "BEGIN CERTIFICATE" not in data:
            raise BackendError(
                "The downloaded OpenVPN configuration is incomplete.")
        return data

    def build_config(self, server: VpnServer, workdir: str,
                     emit: Optional[Emit] = None) -> str:
        """Return the path of a ready to use .ovpn file."""
        use_udp = bool(server.openvpn_udp)
        text = self.decode_config(server)
        if not text:
            if emit is not None:
                text = self.download_config(server, use_udp, emit)
            else:
                text = self.download_config(server, use_udp, lambda *a, **k: None)
        else:
            # The CSV profile may describe a different transport.
            text = re.sub(r"^proto\s+tcp.*$", "proto udp" if use_udp else "proto tcp",
                          text, flags=re.M)

        auth_path = os.path.join(workdir, "auth.txt")
        with open(auth_path, "w") as handle:
            handle.write(f"{VPN_USERNAME}\n{VPN_PASSWORD}\n")
        os.chmod(auth_path, 0o600)

        port = server.openvpn_udp if use_udp else server.openvpn_tcp
        if not port:
            raise BackendError("This server does not offer an OpenVPN port.")

        # Point the client at the numeric address (DDNS is often blocked).
        text = re.sub(r"^remote\s+\S+\s+(\d+)", rf"remote {server.ip} \1",
                      text, flags=re.M)
        text = re.sub(r"^remote\s+\S+\s+\d+", f"remote {server.ip} {port}",
                      text, flags=re.M)
        if use_udp:
            text = re.sub(r"^proto\s+tcp.*$", "proto udp", text, flags=re.M)
        else:
            text = re.sub(r"^proto\s+udp.*$", "proto tcp", text, flags=re.M)

        # Provide the credentials.
        auth_conf_path = auth_path.replace("\\", "/")
        text = re.sub(r"^#?\s*auth-user-pass.*$",
                      lambda _: f'auth-user-pass "{auth_conf_path}"',
                      text, flags=re.M)
        if "auth-user-pass" not in text:
            text += f'\nauth-user-pass "{auth_conf_path}"\n'

        # SoftEther's OpenVPN server only speaks very old dialects.
        text += "\npersist-tun\n"

        path = os.path.join(workdir, "client.ovpn")
        with open(path, "w") as handle:
            handle.write(text)
        os.chmod(path, 0o600)
        return path

    # -- connect / disconnect --------------------------------------------
    def connect(self, server: VpnServer, protocol: Protocol, emit: Emit,
                workdir: str) -> dict:
        config = self.build_config(server, workdir, emit)
        openvpn_bin = self._openvpn_bin() or "openvpn"
        if sys.platform == "win32" and self._is_connect(openvpn_bin):
            return self._connect_openvpn_connect(server, protocol, emit, workdir,
                                                 config, openvpn_bin)

        cmd = [
            openvpn_bin, "--config", config,
            # Pin TLS 1.2: newer OpenVPN clients send a 1.5 kB ClientHello
            # which the SoftEther based relay servers cannot parse.
            "--tls-version-min", "1.2", "--tls-version-max", "1.2",
            "--verb", "3",
        ]
        started = time.monotonic()
        _log_emit(emit, f"Starting OpenVPN ({'UDP' if server.openvpn_udp else 'TCP'} "
                        f"{server.port_for(Protocol.OPENVPN)}) -> {server.ip}")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                bufsize=1)
        deadline = time.time() + CONNECT_TIMEOUT
        connected = False
        lines: List[str] = []
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                lines.append(line)
                _log_emit(emit, line)
                if "Initialization Sequence Completed" in line:
                    connected = True
                    break
                if re.search(r"(^|\W)(ERROR|AUTH_FAILED|Options error)", line):
                    raise BackendError(line.strip())
                if time.time() > deadline:
                    break
        except BackendError:
            self._terminate(proc)
            raise
        if not connected:
            self._terminate(proc)
            tail = "\n".join(lines[-6:])
            raise BackendError(
                "The OpenVPN connection did not complete in time.\n" + tail)

        iface = self._tun_interface()
        ip = netutil.interface_ipv4(iface) if iface else ""
        gateway = netutil.route_via(iface) if iface else None
        if not gateway:
            # The server did not push a default route, install one ourselves.
            gateway = self._fallback_gateway(iface)
            if gateway:
                _log_emit(emit, f"Installing the default route via {gateway} "
                                f"on {iface} ...")
                netutil.install_tunnel_default(gateway, iface, server.ip,
                                               self._physical_gateway())
        _log_emit(emit, f"OpenVPN connected in {_since(started)}: {iface} "
                        f"has {ip or '?'}, gateway {gateway or '?'}")
        return {
            "protocol": protocol.value,
            "server": f"{server.hostname} ({server.ip})",
            "interface": iface,
            "ip": ip,
            "gateway": gateway or "",
            "pid": proc.pid,
            "detail": {"proto": "udp" if server.openvpn_udp else "tcp",
                       "port": server.port_for(Protocol.OPENVPN)},
            "process": proc,
        }

    def _connect_openvpn_connect(self, server: VpnServer, protocol: Protocol,
                                 emit: Emit, workdir: str, config: str,
                                 openvpn_bin: str) -> dict:
        started = time.monotonic()
        _log_emit(emit, f"Importing OpenVPN profile into OpenVPN Connect for {server.hostname or server.ip} ...")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            [openvpn_bin, f"--import-profile={config}", "--name=VPNGate_Tunnel",
             f"--username={VPN_USERNAME}", f"--password={VPN_PASSWORD}"],
            capture_output=True, text=True, timeout=25, check=False,
            creationflags=creation_flags
        )
        profile_id = ""
        try:
            data = json.loads(proc.stdout)
            if data.get("status") == "success":
                profile_id = data.get("message", {}).get("id", "")
        except Exception:
            pass

        if not profile_id:
            proc_list = subprocess.run(
                [openvpn_bin, "--list-profiles"],
                capture_output=True, text=True, timeout=15, check=False,
                creationflags=creation_flags
            )
            try:
                profiles = json.loads(proc_list.stdout)
                for p in profiles:
                    if p.get("name") == "VPNGate_Tunnel":
                        profile_id = p.get("id", "")
                        break
            except Exception:
                pass

        if not profile_id:
            raise BackendError("OpenVPN Connect could not import the server profile.")

        _log_emit(emit, "Starting OpenVPN Connect tunnel ...")
        subprocess.run(
            [openvpn_bin, f"--connect-shortcut={profile_id}", "--minimize"],
            capture_output=True, text=True, timeout=15, check=False,
            creationflags=creation_flags
        )

        deadline = time.time() + CONNECT_TIMEOUT
        ip = ""
        while time.time() < deadline:
            time.sleep(2)
            proc_ip = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-NetIPAddress -InterfaceDescription '*OpenVPN*' -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike '169.254.*' }).IPAddress"],
                capture_output=True, text=True, timeout=10, check=False,
                creationflags=creation_flags
            )
            lines = proc_ip.stdout.strip().splitlines()
            if lines and lines[0].strip():
                ip = lines[0].strip()
                break

        if not ip:
            proc_ip = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-NetIPAddress -InterfaceAlias '*Local Area Connection*' -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike '169.254.*' }).IPAddress"],
                capture_output=True, text=True, timeout=10, check=False,
                creationflags=creation_flags
            )
            lines = proc_ip.stdout.strip().splitlines()
            if lines and lines[0].strip():
                ip = lines[0].strip()

        if not ip:
            subprocess.run([openvpn_bin, "--disconnect-shortcut"], check=False,
                           creationflags=creation_flags)
            subprocess.run([openvpn_bin, f"--remove-profile={profile_id}"], check=False,
                           creationflags=creation_flags)
            raise BackendError("The OpenVPN Connect tunnel did not establish in time.")

        _log_emit(emit, f"OpenVPN Connect established in {_since(started)}: adapter carries {ip}")
        self._last_openvpn_connect_id = profile_id
        return {
            "protocol": protocol.value,
            "server": f"{server.hostname} ({server.ip})",
            "interface": "OpenVPN Connect",
            "ip": ip,
            "gateway": server.ip,
            "detail": {"profile_id": profile_id, "bin": openvpn_bin, "is_connect": True},
        }

    @staticmethod
    def _tun_interface() -> str:
        for name in netutil.list_tunnel_interfaces():
            if name.startswith("tun"):
                return name
        return ""

    @staticmethod
    def _physical_gateway() -> str:
        for gateway, device, _metric in netutil.default_routes():
            if not device.startswith("tun") and not device.startswith("ppp"):
                return gateway
        return ""

    @staticmethod
    def _fallback_gateway(iface: str) -> str:
        """SoftEther relays usually use 10.211.254.254 as the gateway."""
        code, out, _ = netutil.ip_cmd("-4", "route", "show", "dev", iface)
        if code == 0:
            for line in out.splitlines():
                parts = line.split()
                if "via" in parts:
                    return parts[parts.index("via") + 1]
        return ""

    @staticmethod
    def _terminate(proc) -> None:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:                          # noqa: BLE001
            try:
                proc.kill()
            except Exception:                      # noqa: BLE001
                pass

    def disconnect(self, info: dict, emit: Emit) -> None:
        detail = info.get("detail") if isinstance(info.get("detail"), dict) else {}
        openvpn_bin = detail.get("bin") or self._openvpn_bin()
        if sys.platform == "win32" and (detail.get("is_connect") or self._is_connect(openvpn_bin)):
            profile_id = detail.get("profile_id") or getattr(self, "_last_openvpn_connect_id", "")
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run([openvpn_bin, "--disconnect-shortcut"], check=False,
                           creationflags=creation_flags)
            if profile_id:
                subprocess.run([openvpn_bin, f"--remove-profile={profile_id}"], check=False,
                               creationflags=creation_flags)
            _log_emit(emit, "OpenVPN Connect disconnected.")
            return

        proc = info.get("process")
        if proc is not None:
            _log_emit(emit, "Terminating the OpenVPN process ...")
            self._terminate(proc)
        _log_emit(emit, "OpenVPN disconnected.")


# ---------------------------------------------------------------------------
# SSL-VPN (SoftEther VPN client)
# ---------------------------------------------------------------------------

class SoftEtherBackend:
    name = "SSL-VPN (SoftEther)"
    supports = (Protocol.SSLVP,)

    VPNCMD = "vpncmd"
    SERVICE = "/usr/libexec/softether/vpnclient/vpnclient"
    ACCOUNT = "vpngate"
    NIC = "VPN"                             # virtual adapter name

    def available(self) -> bool:
        if sys.platform == "win32":
            return bool(self._vpncmd_bin())
        return bool(netutil.which(self.VPNCMD)) and os.path.exists(self.SERVICE)

    @staticmethod
    def _vpncmd_bin() -> str:
        for name in ("vpncmd", "vpncmd_x64", "vpncmd.exe", "vpncmd_x64.exe"):
            found = shutil.which(name)
            if found:
                return found
        for p in (r"C:\Program Files\SoftEther VPN Client\vpncmd_x64.exe",
                  r"C:\Program Files\SoftEther VPN Client\vpncmd.exe",
                  r"C:\Program Files (x86)\SoftEther VPN Client\vpncmd.exe"):
            if os.path.exists(p):
                return p
        return ""

    # -- vpncmd helpers ---------------------------------------------------
    @staticmethod
    def _run_commands(commands: List[str], timeout: int = 90) -> str:
        workdir = STATE_DIR
        os.makedirs(workdir, exist_ok=True)
        cmd_file = os.path.join(workdir, "vpncmd.cmds")
        out_file = os.path.join(workdir, "vpncmd.out")
        with open(cmd_file, "w") as handle:
            handle.write("\n".join(commands) + "\n")
        # Truncate first: on a slow client (vpncmd can block while a
        # session is negotiating) the stale file from a previous call
        # must not be mistaken for the current result.
        with open(out_file, "w") as handle:
            handle.write("")
        try:
            subprocess.run(
                [self_cmd(), "127.0.0.1", "/CLIENT", f"/IN:{cmd_file}",
                 f"/OUT:{out_file}"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        try:
            with open(out_file) as handle:
                return handle.read()
        except OSError:
            return ""

    def _hard_start(self, emit: Emit) -> None:
        """Restart the client daemon with the TUN module available.

        The SoftEther client keeps a TAP file descriptor per virtual
        adapter; once a session churn leaves that fd stale every later
        attach fails with "code 22" until the daemon is restarted.  Note
        that "vpnclient stop" does not actually stop the workers: there
        can be many "execsvc" pairs from earlier starts, so a forced
        kill that is verified is the only reliable reset.
        """
        netutil.run(["systemctl", "stop", "softether-vpnclient"], timeout=30)
        netutil.run(["pkill", "-9", "-x", "vpnclient"], timeout=15)
        for _ in range(10):
            code, _, _err = netutil.run(["pgrep", "-x", "vpnclient"], timeout=10)
            if code != 0:
                break
            time.sleep(1)
        netutil.run(["modprobe", "tun"], timeout=10)
        code, _, _ = netutil.run(["systemctl", "start", "softether-vpnclient"],
                                 timeout=60)
        if code != 0:
            netutil.run([self.SERVICE, "start"], timeout=60)
        time.sleep(4)
        _log_emit(emit, "SoftEther client daemon restarted (TUN ready).")

    def _iface(self) -> str:
        """Kernel-side name of the virtual adapter (SoftEther lowercases)."""
        return f"vpn_{self.NIC}".lower()

    def _reset_nic(self, nic: str, emit: Emit) -> str:
        """Drop the virtual adapter's registry entry and create it again.

        The daemon itself owns the kernel device (it recreates
        ``vpn_<nic>`` on start from its persisted config), so the kernel
        side is left alone; touching it mid-writer is what used to leave
        the adapter wedged.
        """
        self._run_commands([f"NicDelete {nic}"], timeout=30)
        out = self._run_commands([f"NicCreate {nic}", f"NicEnable {nic}"],
                                 timeout=60)
        if "successfully" not in out.lower():
            _log_emit(emit, f"NicCreate {nic} may have failed: "
                            f"{out.strip().splitlines()[-1] if out else '?'}")
        iface = self._iface()
        for _ in range(6):
            if netutil.interface_up(iface):
                break
            time.sleep(1)
        return nic

    def _session_and_adapter(self, emit: Emit) -> bool:
        """Wait for the virtual adapter to come up.

        The kernel tap is attached to the session on connect, so its
        presence is the reliable evidence that the dial succeeded;
        vpncmd status answers can block for tens of seconds right at
        that moment and must not gate the flow.  A zombie session is
        caught later by the DHCP phase failing.
        """
        iface = self._iface()
        deadline = time.time() + 30
        while time.time() < deadline:
            if netutil.interface_up(iface):
                return True
            time.sleep(2)
        return False

    def _install_dns(self, iface: str, servers: List[str], emit: Emit) -> None:
        if not servers or not netutil.which("resolvectl"):
            if servers:
                _log_emit(emit, "DNS was not changed (resolvectl unavailable).")
            return
        _log_emit(emit, f"DNS on {iface} -> {', '.join(servers)} "
                        "(resolvectl, scoped to the tunnel)")
        netutil.run(["resolvectl", "dns", iface, *servers], timeout=10)
        netutil.run(["resolvectl", "route", iface],
                    timeout=10)

    def connect(self, server: VpnServer, protocol: Protocol, emit: Emit,
                workdir: str) -> dict:
        if sys.platform == "win32":
            return self._connect_windows(server, protocol, emit, workdir)

        port = server.ssl_vpn_port
        if not port:
            raise BackendError("This server does not offer the SSL-VPN service.")
        host = server.hostname or server.ip
        target = f"{host}:{port}"
        started = time.monotonic()

        _log_emit(emit, f"[sslvpn 1/5] Starting SSL-VPN (SoftEther) -> {target} "
                        f"[hub {SOFTETHER_HUB}, user {VPN_USERNAME}]")
        _log_emit(emit, "[sslvpn 2/5] Restarting the client daemon so the TUN "
                        "device and virtual adapters are clean ...")
        self._hard_start(emit)

        last_error = ""
        for attempt in (1, 2):
            if attempt == 2:
                _log_emit(emit, "[sslvpn] second attempt after another full "
                                "daemon restart ...")
                self._hard_start(emit)
            _log_emit(emit, f"[sslvpn 3/5] Dial attempt {attempt}/2 -> {target} "
                            "(fresh virtual NIC + account)")
            self._reset_nic(self.NIC, emit)
            commands = [
                f"AccountDisconnect {self.ACCOUNT}",
                f"AccountDelete {self.ACCOUNT}",
                f"AccountCreate {self.ACCOUNT} /SERVER:{target} "
                f"/HUB:{SOFTETHER_HUB} /USERNAME:{VPN_USERNAME} "
                f"/NICNAME:{self.NIC}",
                f"AccountPasswordSet {self.ACCOUNT} /PASSWORD:{VPN_PASSWORD} "
                f"/TYPE:standard",
                f"AccountServerCertDisable {self.ACCOUNT}",
                f"AccountStatusHide {self.ACCOUNT}",
                f"AccountConnect {self.ACCOUNT}",
            ]
            self._run_commands(commands, timeout=90)
            if self._session_and_adapter(emit):
                break
            last_error = "the session or the virtual adapter did not come up"
            self._run_commands([f"AccountDisconnect {self.ACCOUNT}"],
                               timeout=40)
        else:
            self._run_commands([f"AccountDelete {self.ACCOUNT}"], timeout=40)
            raise BackendError(f"The SSL-VPN session was not established "
                               f"({last_error}).")

        nic = self.NIC
        iface = self._iface()
        _log_emit(emit, f"[sslvpn 3/5] Session established; the kernel adapter "
                        f"{iface} is live ({_since(started)} into this dial)")
        lease = self._dhcp(iface, emit)
        ip = lease.get("ip", "") if lease else ""
        gateway = (lease.get("gateway") or netutil.route_via(iface)) \
            if lease else None
        if not gateway:
            _log_emit(emit, f"Disconnecting {target} (no DHCP lease from the "
                            f"server's SecureNAT).")
            self.disconnect({"interface": iface}, emit)
            raise BackendError(
                "The server did not hand out a VPN address: its virtual DHCP "
                "(SecureNAT) did not answer. Try another server; relays often "
                "have this broken today.")
        if lease and lease.get("dns"):
            self._install_dns(iface, lease["dns"], emit)
        physical = self._physical_gateway()
        _log_emit(emit, f"[sslvpn 4/5] Installing the tunnel default route: "
                        f"via {gateway} dev {iface} "
                        f"(your old default via {physical or '?'} stays reachable)")
        netutil.install_tunnel_default(gateway, iface, server.ip, physical)
        _log_emit(emit, f"[sslvpn 5/5] Connected in {_since(started)}: "
                        f"{iface} carries {ip or '?'}, gateway {gateway}")
        return {
            "protocol": protocol.value,
            "server": f"{server.hostname} ({server.ip})",
            "interface": iface,
            "ip": ip,
            "gateway": gateway or "",
            "detail": {"port": port, "nic": nic},
        }

    @staticmethod
    def _dhcp(iface: str, emit: Emit) -> Optional[dict]:
        """Request an address for the SoftEther virtual adapter.

        The minimal in-tree DHCP client is the primary path: it exchanges
        directly on the adapter and reports gateway/DNS it received.  The
        dhcpcd one-shot is only a fallback when the relays' SecureNAT
        speaks a dialect the simple client mishandles.

        Addresses in 169.254/16 are link-local self-assignments (dhcpcd
        falls back to them when the DHCP server is dead) and are treated
        as "no lease" - otherwise a relay with a broken SecureNAT is
        reported as connected without any connectivity.
        """
        for attempt in (1, 2):
            step = time.monotonic()
            lease = netutil.dhcp_request(iface, timeout=12)
            ip = lease.get("ip") if lease else None
            if ip and not ip.startswith("169.254."):
                netutil.configure_interface(iface, lease)
                _log_emit(emit, f"DHCP lease in {_since(step)}: "
                                f"{lease['ip']}/{lease['netmask']} "
                                f"gateway {lease.get('gateway')} "
                                f"DNS {', '.join(lease.get('dns') or []) or '?'}")
                return lease
            _log_emit(emit, f"No usable DHCP lease on {iface} "
                            f"(attempt {attempt}/2, {_since(step)}; link-local "
                            "addresses are ignored on purpose)")
            time.sleep(3)
        if netutil.which("dhcpcd"):
            step = time.monotonic()
            code, out, err = netutil.run(
                ["dhcpcd", "-1", "-B", "--nohook", "resolv.conf", "-t", "30",
                 iface], timeout=45)
            ip = netutil.interface_ipv4(iface)
            if ip and not ip.startswith("169.254."):
                _log_emit(emit, f"DHCP lease via dhcpcd in {_since(step)}: {ip}")
                return {"ip": ip, "gateway": netutil.route_via(iface)}
        # A lingering link-local address would confuse later attempts.
        netutil.run(["ip", "addr", "flush", "dev", iface], timeout=10)
        _log_emit(emit, f"No DHCP lease on {iface} "
                        "(the server's virtual DHCP did not answer).")
        return None

    @staticmethod
    def _physical_gateway() -> str:
        for gateway, device, _metric in netutil.default_routes():
            if not device.startswith(("tun", "ppp", "vpn_")):
                return gateway
        return ""

    @staticmethod
    def _parse_status(output: str) -> tuple:
        """Return (status text, is_connected).

        Depending on the state the SoftEther client reports either
        "Session Status | Connection Completed" or the
        "Current Session has been Established since | <date>" line.
        """
        match = re.search(r"Session Status\s*\|\s*(.+)", output)
        status = match.group(1).strip() if match else ""
        if "Connection Completed" in status:
            return status, True
        match = re.search(
            r"Current Session has been Established since\s*\|\s*(.+)", output)
        if match and match.group(1).strip() not in ("-", ""):
            return status or "connected", True
        match = re.search(r"Number of Established Sessions\s*\|\s*(\d+)", output)
        if match and int(match.group(1)) > 0 and "Current Session" in output:
            return status or "connected", True
        return status, False

    def _connect_windows(self, server: VpnServer, protocol: Protocol, emit: Emit,
                         workdir: str) -> dict:
        port = server.ssl_vpn_port
        if not port:
            raise BackendError("This server does not offer the SSL-VPN service.")
        host = server.hostname or server.ip
        target = f"{host}:{port}"
        started = time.monotonic()
        _log_emit(emit, f"[sslvpn] Starting SSL-VPN (SoftEther) -> {target} [hub {SOFTETHER_HUB}, user {VPN_USERNAME}]")

        # Ensure Virtual NIC exists in SoftEther
        self._run_commands([f"NicCreate {self.NIC}", f"NicEnable {self.NIC}"], timeout=30)

        # Clear any stale vpngate account session and dial
        commands = [
            f"AccountDisconnect {self.ACCOUNT}",
            f"AccountDelete {self.ACCOUNT}",
            f"AccountCreate {self.ACCOUNT} /SERVER:{target} /HUB:{SOFTETHER_HUB} /USERNAME:{VPN_USERNAME} /NICNAME:{self.NIC}",
            f"AccountPasswordSet {self.ACCOUNT} /PASSWORD:{VPN_PASSWORD} /TYPE:standard",
            f"AccountServerCertDisable {self.ACCOUNT}",
            f"AccountStatusHide {self.ACCOUNT}",
            f"AccountConnect {self.ACCOUNT}",
        ]
        self._run_commands(commands, timeout=40)
        _log_emit(emit, f"[sslvpn] Dialing {target} ...")

        deadline = time.time() + 35
        established = False
        while time.time() < deadline:
            out = self._run_commands([f"AccountStatusGet {self.ACCOUNT}"], timeout=15)
            _status, is_connected = self._parse_status(out)
            if is_connected:
                established = True
                break
            if "Error code: 43" in out:
                raise BackendError("The SoftEther Virtual Network Adapter is already in use by another VPN connection.")
            time.sleep(1.5)

        if not established:
            self._run_commands([f"AccountDisconnect {self.ACCOUNT}"], timeout=20)
            raise BackendError("The SSL-VPN session was not established in time.")

        _log_emit(emit, f"[sslvpn] Session established in {_since(started)}; waiting for IP address ...")

        ip = ""
        ip_deadline = time.time() + 15
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        while time.time() < ip_deadline:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-NetIPAddress -InterfaceAlias '*VPN*' -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike '169.254.*' }).IPAddress"],
                capture_output=True, text=True, timeout=10, check=False,
                creationflags=creation_flags
            )
            val = proc.stdout.strip().splitlines()
            if val and val[0].strip():
                ip = val[0].strip()
                break
            time.sleep(1)

        _log_emit(emit, f"[sslvpn] Connected in {_since(started)}: adapter carries {ip or 'DHCP assigned'}")
        return {
            "protocol": protocol.value,
            "server": f"{server.hostname} ({server.ip})",
            "interface": "VPN",
            "ip": ip or target,
            "gateway": target,
            "detail": {"account": self.ACCOUNT, "port": port},
        }

    def disconnect(self, info: dict, emit: Emit) -> None:
        if sys.platform == "win32":
            _log_emit(emit, "[sslvpn] Closing the SoftEther session ...")
            self._run_commands([f"AccountDisconnect {self.ACCOUNT}",
                                f"AccountDelete {self.ACCOUNT}"], timeout=20)
            _log_emit(emit, "SSL-VPN disconnected.")
            return

        started = time.monotonic()
        _log_emit(emit, "[sslvpn] Closing the SoftEther session ...")
        self._run_commands([f"AccountDisconnect {self.ACCOUNT}"], timeout=60)
        self._run_commands([f"AccountDelete {self.ACCOUNT}",
                            f"NicDelete {self.NIC}"], timeout=60)
        iface = info.get("interface") or self._iface()
        gateway = info.get("gateway", "")
        if iface and gateway:
            _log_emit(emit, f"[sslvpn] Removing the tunnel default route "
                            f"(via {gateway} dev {iface}) ...")
            netutil.remove_tunnel_default(gateway, iface)
        netutil.run(["ip", "route", "flush", "dev", iface], timeout=10)
        netutil.run(["ip", "link", "del", iface], timeout=10)
        if netutil.which("resolvectl"):
            _log_emit(emit, f"[sslvpn] Reverting the system DNS configuration "
                            f"for {iface} ...")
            netutil.run(["resolvectl", "revert", iface], timeout=10)
        _log_emit(emit, f"SSL-VPN disconnected in {_since(started)}.")


def self_cmd() -> str:
    if sys.platform == "win32":
        return SoftEtherBackend._vpncmd_bin() or "vpncmd"
    return shutil.which("vpncmd") or "/usr/bin/vpncmd"


# ---------------------------------------------------------------------------
# MS-SSTP
# ---------------------------------------------------------------------------

class SstpBackend:
    name = "MS-SSTP"
    supports = (Protocol.SSTP,)

    def available(self) -> bool:
        if sys.platform == "win32":
            return bool(shutil.which("rasdial") or os.path.exists(r"C:\Windows\System32\rasdial.exe"))
        return bool(netutil.which("sstpc")) and bool(netutil.which("pppd"))

    @staticmethod
    def _configure_windows_vpn_pbk(vpn_name: str) -> None:
        try:
            pbk_path = os.path.expandvars(r"%APPDATA%\Microsoft\Network\Connections\Pbk\rasphone.pbk")
            if not os.path.isfile(pbk_path):
                return
            with open(pbk_path, "r", encoding="utf-8", errors="ignore") as handle:
                content = handle.read()
            def fix_section(match):
                block = match.group(0)
                block = re.sub(r"IpPrioritizeRemote=\d+", "IpPrioritizeRemote=1", block)
                block = re.sub(r"IpInterfaceMetric=\d+", "IpInterfaceMetric=1", block)
                block = re.sub(r"Ipv6PrioritizeRemote=\d+", "Ipv6PrioritizeRemote=1", block)
                block = re.sub(r"Ipv6InterfaceMetric=\d+", "Ipv6InterfaceMetric=1", block)
                return block
            pattern = rf"\[{re.escape(vpn_name)}\][\s\S]*?(?=\n\[|\Z)"
            updated = re.sub(pattern, fix_section, content)
            with open(pbk_path, "w", encoding="utf-8") as handle:
                handle.write(updated)
        except Exception:
            pass

    def _connect_windows(self, server: VpnServer, protocol: Protocol, emit: Emit,
                         workdir: str) -> dict:
        host = server.sstp_host or server.hostname or server.ip
        if host and "." not in host:
            host = f"{host}.opengw.net"

        vpn_name = "VPNGate"
        _log_emit(emit, f"[sstp 1/3] Configuring native Windows MS-SSTP connection to {host} ...")
        started = time.monotonic()

        subprocess.run(["rasdial", vpn_name, "/DISCONNECT"],
                       capture_output=True, text=True, timeout=10)

        ps_add = (
            f"Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force; "
            f"Remove-VpnConnection -Name '{vpn_name}' -Force -ErrorAction SilentlyContinue; "
            f"Add-VpnConnection -Name '{vpn_name}' -ServerAddress '{host}' -TunnelType Sstp "
            f"-EncryptionLevel Optional -AuthenticationMethod Pap,Chap,MSChapv2 "
            f"-SplitTunneling:$false -Force"
        )
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_add],
            capture_output=True, text=True, timeout=30
        )
        if res.returncode != 0:
            err = res.stderr.strip() or res.stdout.strip()
            raise BackendError(f"Failed to create Windows VPN profile for MS-SSTP: {err}")
        self._configure_windows_vpn_pbk(vpn_name)

        _log_emit(emit, f"[sstp 2/3] Dialing {vpn_name} ({host}) via Windows RAS ...")
        dial_res = subprocess.run(
            ["rasdial", vpn_name, VPN_USERNAME, VPN_PASSWORD],
            capture_output=True, text=True, timeout=60
        )
        output = (dial_res.stdout or "") + (dial_res.stderr or "")
        for line in output.splitlines():
            line = line.strip()
            if line:
                _log_emit(emit, f"[rasdial] {line}")

        if dial_res.returncode != 0 or "successfully connected" not in output.lower():
            subprocess.run(["rasdial", vpn_name, "/DISCONNECT"],
                           capture_output=True, text=True, timeout=10)
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            f"Remove-VpnConnection -Name '{vpn_name}' -Force -ErrorAction SilentlyContinue"],
                           capture_output=True, text=True, timeout=10)
            last_line = output.strip().splitlines()[-1] if output.strip() else "connection failed"
            raise BackendError(f"MS-SSTP connection failed: {last_line}")

        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"Set-NetIPInterface -InterfaceAlias '{vpn_name}' -InterfaceMetric 1 -ErrorAction SilentlyContinue"],
            capture_output=True, text=True, timeout=10
        )
        _log_emit(emit, f"[sstp 3/3] Connected in {_since(started)}: Windows MS-SSTP tunnel is active")
        return {
            "protocol": protocol.value,
            "server": f"{server.hostname or host} ({server.ip})",
            "interface": vpn_name,
            "ip": server.ip,
            "gateway": host,
            "detail": {"vpn_name": vpn_name, "host": host},
            "windows_vpn": vpn_name,
        }

    def _write_ppp_options(self, workdir: str) -> str:
        secrets = os.path.join(workdir, "pap-secrets")
        with open(secrets, "w") as handle:
            handle.write(f"* * {VPN_PASSWORD} *\n")
        os.chmod(secrets, 0o600)
        options = os.path.join(workdir, "ppp-options")
        with open(options, "w") as handle:
            # ppp >= 2.5 uses "refuse-<method>" to select the method we are
            # willing to authenticate ourselves with.  The VPN Gate servers
            # expect plain PAP.
            handle.write(
                "noauth\n"
                "refuse-eap\n"
                "refuse-chap\n"
                "refuse-mschap\n"
                "refuse-mschap-v2\n"
                f"pap-secrets {secrets}\n"
                "chap-secrets /etc/ppp/chap-secrets\n"
                f"user {VPN_USERNAME}\n"
                f"password {VPN_PASSWORD}\n"
                "noipdefault\n"
                "usepeerdns\n"
                "nodefaultroute\n"
                "noaccomp\n"
            )
        return options

    def connect(self, server: VpnServer, protocol: Protocol, emit: Emit,
                workdir: str) -> dict:
        if sys.platform == "win32":
            return self._connect_windows(server, protocol, emit, workdir)
        port = server.sstp_port
        if not port:
            raise BackendError("This server does not offer the MS-SSTP service.")
        host = server.sstp_host or server.hostname or server.ip
        target = f"{host}:{port}"

        options = self._write_ppp_options(workdir)
        log_path = os.path.join(workdir, "sstp.log")
        cmd = [
            "pppd", "nodetach", "pty",
            f"sstpc --nolaunchpppd --user {VPN_USERNAME} "
            f"--password {VPN_PASSWORD} --cert-warn --log-stderr "
            f"--log-level INFO {target}",
            "file", options,
        ]
        _log_emit(emit, f"[sstp 1/3] Starting MS-SSTP -> {target} "
                        "(HTTPS tunnel, PAP profile vpn/vpn will be used)")
        started = time.monotonic()
        with open(log_path, "w") as logfile:
            proc = subprocess.Popen(cmd, stdout=logfile, stderr=logfile,
                                    stdin=subprocess.DEVNULL)

        deadline = time.time() + CONNECT_TIMEOUT
        iface = ""
        tick = 0
        while time.time() < deadline:
            iface = self._ppp_interface()
            if iface and netutil.interface_ipv4(iface):
                break
            if proc.poll() is not None:
                break
            tick += 1
            if tick % 5 == 0:                     # every ~10 s
                _log_emit(emit, f"[sstp] waiting for the ppp handshake "
                                f"({_since(started)} elapsed) ...")
            time.sleep(2)

        if not iface or not netutil.interface_ipv4(iface):
            proc.terminate()
            try:
                with open(log_path) as handle:
                    tail = "\n".join(handle.read().splitlines()[-8:])
            except OSError:
                tail = ""
            raise BackendError("The MS-SSTP tunnel could not be established.\n" + tail)

        ip = netutil.interface_ipv4(iface)
        gateway = self._fallback_gateway(iface)
        if gateway:
            _log_emit(emit, f"[sstp 2/3] ppp peer gateway is {gateway}; installing "
                            f"the tunnel default route (old default kept)")
            netutil.install_tunnel_default(gateway, iface, server.ip,
                                           self._physical_gateway())
        _log_emit(emit, f"[sstp 3/3] Connected in {_since(started)}: "
                        f"{iface} carries {ip or '?'}")
        return {
            "protocol": protocol.value,
            "server": f"{server.hostname} ({server.ip})",
            "interface": iface,
            "ip": ip or "",
            "gateway": gateway or "",
            "detail": {"port": port, "log": log_path},
            "process": proc,
        }

    @staticmethod
    def _ppp_interface() -> str:
        for name in netutil.list_tunnel_interfaces():
            if name.startswith("ppp"):
                return name
        return ""

    @staticmethod
    def _fallback_gateway(iface: str) -> str:
        """The PPP peer address acts as the tunnel's gateway."""
        peer = netutil.interface_peer_ipv4(iface)
        if peer:
            return peer
        code, out, _ = netutil.ip_cmd("-4", "route", "show", "dev", iface)
        if code == 0:
            for line in out.splitlines():
                parts = line.split()
                if "via" in parts:
                    return parts[parts.index("via") + 1]
        return ""

    @staticmethod
    def _physical_gateway() -> str:
        for gateway, device, _metric in netutil.default_routes():
            if not device.startswith(("tun", "ppp", "vpn_")):
                return gateway
        return ""

    def disconnect(self, info: dict, emit: Emit) -> None:
        if sys.platform == "win32":
            vpn_name = info.get("windows_vpn") or "VPNGate"
            _log_emit(emit, f"[sstp] Disconnecting Windows RAS connection {vpn_name} ...")
            subprocess.run(["rasdial", vpn_name, "/DISCONNECT"],
                           capture_output=True, text=True, timeout=15)
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            f"Remove-VpnConnection -Name '{vpn_name}' -Force -ErrorAction SilentlyContinue"],
                           capture_output=True, text=True, timeout=15)
            _log_emit(emit, "Windows MS-SSTP disconnected.")
            return
        started = time.monotonic()
        proc = info.get("process")
        if proc is not None:
            _log_emit(emit, "[sstp] Terminating the sstpc/pppd pair ...")
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:                      # noqa: BLE001
                try:
                    proc.kill()
                except Exception:                  # noqa: BLE001
                    pass
        iface = info.get("interface")
        gateway = info.get("gateway")
        if iface and gateway:
            _log_emit(emit, f"[sstp] Removing the tunnel default route "
                            f"(via {gateway} dev {iface}) ...")
            netutil.remove_tunnel_default(gateway, iface)
        _log_emit(emit, f"MS-SSTP disconnected in {_since(started)}.")


# ---------------------------------------------------------------------------
# L2TP / IPsec
# ---------------------------------------------------------------------------

class L2tpBackend:
    name = "L2TP/IPsec"
    supports = (Protocol.L2TP,)

    XL2TPD_CONF = "/etc/xl2tpd/xl2tpd.conf"
    PPP_OPTIONS = "/etc/ppp/options.vpngate"
    SWANCTL_CONF = "/etc/swanctl/conf.d/vpngate.conf"   # connections{} + secrets{}
    VICI_SOCKET = "/run/charon.vici"
    L2TP_CONTROL = "/var/run/xl2tpd/l2tp-control"
    LAC_NAME = "vpngate"
    IKE_NAME = "vpngate"
    CHILD_NAME = "vpngate-esp"
    IPSEC_TIMEOUT = 45
    CONNECT_TIMEOUT = 60                    # total wait for the ppp interface
    FIX_QUEUE = 7                           # NFQUEUE used by the csumfix daemon
    _charon_proc = None                     # set when we spawn charon ourselves
    _fixer_proc = None                      # UDP-checksum fixer daemon

    def available(self) -> bool:
        if sys.platform == "win32":
            return bool(shutil.which("rasdial") or os.path.exists(r"C:\Windows\System32\rasdial.exe"))
        return bool(netutil.which("xl2tpd")) and bool(netutil.which("swanctl"))

    def _connect_windows(self, server: VpnServer, protocol: Protocol, emit: Emit,
                         workdir: str) -> dict:
        host = server.ip or server.hostname
        vpn_name = "VPNGate"
        _log_emit(emit, f"[l2tp 1/3] Configuring native Windows L2TP/IPsec connection to {host} ...")
        started = time.monotonic()

        subprocess.run(["rasdial", vpn_name, "/DISCONNECT"],
                       capture_output=True, text=True, timeout=10)

        ps_add = (
            f"Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force; "
            f"Remove-VpnConnection -Name '{vpn_name}' -Force -ErrorAction SilentlyContinue; "
            f"Add-VpnConnection -Name '{vpn_name}' -ServerAddress '{host}' -TunnelType L2tp "
            f"-L2tpPsk '{VPN_PSK}' -EncryptionLevel Optional -AuthenticationMethod Pap,Chap,MSChapv2 "
            f"-SplitTunneling:$false -Force"
        )
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_add],
            capture_output=True, text=True, timeout=30
        )
        if res.returncode != 0:
            err = res.stderr.strip() or res.stdout.strip()
            raise BackendError(f"Failed to create Windows VPN profile for L2TP/IPsec: {err}")
        SstpBackend._configure_windows_vpn_pbk(vpn_name)

        _log_emit(emit, f"[l2tp 2/3] Dialing {vpn_name} ({host}) via Windows RAS with PSK ...")
        dial_res = subprocess.run(
            ["rasdial", vpn_name, VPN_USERNAME, VPN_PASSWORD],
            capture_output=True, text=True, timeout=60
        )
        output = (dial_res.stdout or "") + (dial_res.stderr or "")
        for line in output.splitlines():
            line = line.strip()
            if line:
                _log_emit(emit, f"[rasdial] {line}")

        if dial_res.returncode != 0 or "successfully connected" not in output.lower():
            subprocess.run(["rasdial", vpn_name, "/DISCONNECT"],
                           capture_output=True, text=True, timeout=10)
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            f"Remove-VpnConnection -Name '{vpn_name}' -Force -ErrorAction SilentlyContinue"],
                           capture_output=True, text=True, timeout=10)
            last_line = output.strip().splitlines()[-1] if output.strip() else "connection failed"
            raise BackendError(f"L2TP/IPsec connection failed: {last_line}")

        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"Set-NetIPInterface -InterfaceAlias '{vpn_name}' -InterfaceMetric 1 -ErrorAction SilentlyContinue"],
            capture_output=True, text=True, timeout=10
        )

        _log_emit(emit, f"[l2tp 3/3] Connected in {_since(started)}: Windows L2TP/IPsec tunnel is active")
        return {
            "protocol": protocol.value,
            "server": f"{server.hostname or host} ({server.ip})",
            "interface": vpn_name,
            "ip": server.ip,
            "gateway": host,
            "detail": {"vpn_name": vpn_name, "host": host},
            "windows_vpn": vpn_name,
        }

    # -- inbound UDP-checksum workaround -------------------------------------
    # Cheap SOHO routers NAT the ESP-in-UDP flow without refreshing the UDP
    # checksum, which makes the kernel drop every inbound datagram.  The
    # fixer daemon hooks inbound udp/4500 via NFQUEUE and refreshes stale
    # checksums before the IPsec demux sees them.
    @staticmethod
    def _nfqueue_rule(action: str) -> None:
        cmd = ["iptables", "-t", "mangle", action, "PREROUTING"]
        if action == "-I":
            cmd.append("1")
        cmd += ["-p", "udp", "--dport", "4500",
                "-j", "NFQUEUE", "--queue-num", str(L2tpBackend.FIX_QUEUE)]
        netutil.run(cmd, timeout=10)

    def _install_fixer(self, emit: Emit) -> None:
        try:
            import netfilterqueue           # noqa: F401
        except ImportError:
            _log_emit(emit,
                      "Note: python3-netfilterqueue missing; if the local "
                      "router rewrites ESP-in-UDP checksums the inbound "
                      "side may stay silent.")
            return
        self._nfqueue_rule("-D")            # clear any stale rule first
        self._nfqueue_rule("-I")
        if (type(self)._fixer_proc is None
                or type(self)._fixer_proc.poll() is not None):
            _log_emit(emit, f"[l2tp] Starting the ESP-in-UDP checksum fixer "
                            f"(NFQUEUE {self.FIX_QUEUE}) ...")
            type(self)._fixer_proc = subprocess.Popen(
                [sys.executable, os.path.join(os.path.dirname(__file__),
                                              "csumfix.py")],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL)

    @classmethod
    def _remove_fixer(cls) -> None:
        cls._nfqueue_rule("-D")
        proc = cls._fixer_proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
        cls._fixer_proc = None

    # -- strongSwan (swanctl / vici) ----------------------------------------
    def _write_swanctl(self, server: VpnServer) -> None:
        """Drop an IKEv1 transport-mode PSK connection + secret for the LNS."""
        os.makedirs(os.path.dirname(self.SWANCTL_CONF), exist_ok=True)
        ike_proposals = (
            "aes256-sha1-modp2048, aes256-sha1-modp1024, "
            "aes128-sha1-modp2048, aes128-sha1-modp1024, 3des-sha1-modp1024")
        with open(self.SWANCTL_CONF, "w") as handle:
            handle.write(
                "connections {\n"
                f"\t{self.IKE_NAME} {{\n"
                "\t\tversion = 1\n"
                f"\t\tremote_addrs = {server.ip}\n"
                "\t\tlocal {\n"
                "\t\t\tauth = psk\n"
                "\t\t\tid = %any\n"
                "\t\t}\n"
                "\t\tremote {\n"
                "\t\t\tauth = psk\n"
                "\t\t\tid = %any\n"
                "\t\t}\n"
                f"\t\tproposals = {ike_proposals}\n"
                "\t\tencap = yes\n"
                "\t\tdpd_delay = 30s\n"
                "\t\tdpd_timeout = 120s\n"
                "\t\tmobike = no\n"
                "\t\treauth_time = 0s\n"
                "\t\trekey_time = 4h\n"
                "\t\tchildren {\n"
                f"\t\t\t{self.CHILD_NAME} {{\n"
                "\t\t\t\tmode = transport\n"
                "\t\t\t\tlocal_ts = dynamic[udp/1701]\n"
                f"\t\t\t\tremote_ts = {server.ip}[udp/1701]\n"
                "\t\t\t\tesp_proposals = aes256-sha1, aes128-sha1, 3des-sha1\n"
                "\t\t\t\trekey_time = 55m\n"
                "\t\t\t\tlife_time = 60m\n"
                "\t\t\t\tstart_action = none\n"
                "\t\t\t\tclose_action = none\n"
                "\t\t\t}\n"
                "\t\t}\n"
                "\t}\n"
                "}\n"
                "secrets {\n"
                f"\tike{self.IKE_NAME} {{\n"
                "\t\tsecret = \"" + VPN_PSK + "\"\n"
                "\t}\n"
                "}\n"
            )
        os.chmod(self.SWANCTL_CONF, 0o600)

    @staticmethod
    def _vici_alive() -> bool:
        """True only when the vici socket answers (stale files don't count)."""
        if not os.path.exists(L2tpBackend.VICI_SOCKET):
            return False
        import socket
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(2)
                sock.connect(L2tpBackend.VICI_SOCKET)
            return True
        except OSError:
            try:
                os.unlink(L2tpBackend.VICI_SOCKET)
            except OSError:                        # noqa: BLE001
                pass
            return False

    def _start_charon(self, emit: Emit) -> bool:
        """Make sure the charon vici channel exists (starts strongswan-swanctl)."""
        if self._vici_alive():
            return True
        _log_emit(emit, "Starting the IPsec daemon (strongSwan) ...")
        netutil.run(["systemctl", "start", "strongswan-swanctl"], timeout=30)
        deadline = time.time() + 12
        while time.time() < deadline:
            if self._vici_alive():
                return True
            time.sleep(1)
        # Ubuntu 26.04 ships strongSwan without systemd units: spawn the
        # daemon ourselves.
        charon = "/usr/lib/ipsec/charon"
        if os.path.exists(charon):
            try:
                self._charon_proc = subprocess.Popen(
                    [charon, "--use-syslog"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL)
            except OSError:
                pass
            deadline = time.time() + 12
            while time.time() < deadline:
                if self._vici_alive():
                    return True
                if self._charon_proc and self._charon_proc.poll() is not None:
                    break
                time.sleep(1)
        return False

    def _ipsec_up(self, server: VpnServer, emit: Emit) -> None:
        started = time.monotonic()
        if not self._start_charon(emit):
            raise BackendError(
                "The strongSwan IPsec daemon (charon / vici) could not be started.")
        self._write_swanctl(server)
        _log_emit(emit, "[l2tp] Loading the IKEv1 connection definition into "
                        "swanctl (transport mode ESP for UDP/1701) ...")
        code, out, err = netutil.run(["swanctl", "--load-all"], timeout=30)
        _log_emit(emit, (out.strip() or err.strip()) or "swanctl: nothing loaded")
        if code != 0 or "loaded" not in (out + err).lower():
            raise BackendError(
                "strongSwan rejected the L2TP/IPsec definition.\n"
                f"{out.strip()} {err.strip()}")

        _log_emit(emit, f"[l2tp] Initiating the IKEv1 Main Mode exchange "
                        f"towards {server.ip} (3 attempts, 25 s each) ...")
        combined = ""
        attempt_no = 0
        for attempt in (1, 2, 3):
            attempt_no = attempt
            code, out, err = netutil.run(
                ["swanctl", "--initiate", "--ike", self.IKE_NAME,
                 "--child", self.CHILD_NAME, "--timeout", "25"],
                timeout=self.IPSEC_TIMEOUT + 15)
            combined = (out + err).lower()
            _log_emit(emit, f"[l2tp] IKE attempt {attempt}/3: "
                            f"{(out.strip() or err.strip()).splitlines()[-1] if (out.strip() or err.strip()) else 'no response'}")
            succeeded = code == 0 and (
                "established" in combined or "completed successfully" in combined)
            if succeeded:
                code, out2, _err2 = netutil.run(["ip", "xfrm", "state"])
                if "esp" in (out2 or "").lower():
                    break                   # kernel state installed for SAs
                _log_emit(emit, "[l2tp] SA reported established but no ESP "
                                "state in the kernel; retrying ...")
            netutil.run(["swanctl", "--terminate", "--ike", self.IKE_NAME],
                        timeout=15)
            time.sleep(4)
        else:
            self._ipsec_down()
            raise BackendError(
                "The IPsec SA could not be established.\n"
                f"{(out + err).strip()}")
        _log_emit(emit, f"[l2tp] IPsec transport SA up after {attempt_no}/3 "
                        f"attempts (IKE + ESP child, {_since(started)})")

    def _ipsec_down(self) -> None:
        netutil.run(["swanctl", "--terminate", "--ike", L2tpBackend.IKE_NAME],
                    timeout=30)
        proc = type(self)._charon_proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    # -- xl2tpd / PPP -------------------------------------------------------
    def _write_config(self, server: VpnServer) -> None:
        os.makedirs(os.path.dirname(self.XL2TPD_CONF), exist_ok=True)
        os.makedirs(os.path.dirname(self.L2TP_CONTROL), exist_ok=True)
        with open(self.XL2TPD_CONF, "w") as handle:
            handle.write(
                "[global]\n"
                "port = 1701\n"
                "force userspace = yes\n"
                "\n"
                f"[lac {self.LAC_NAME}]\n"
                f"lns = {server.ip}\n"
                "require chap = yes\n"
                "refuse pap = yes\n"
                "require authentication = yes\n"
                f"name = {VPN_USERNAME}\n"
                "autodial = no\n"
                "redial = no\n"
                f"pppoptfile = {self.PPP_OPTIONS}\n"
            )
        # The relay's PPP peer authenticates us with MSCHAPv2 (login vpn/vpn).
        with open(self.PPP_OPTIONS, "w") as handle:
            handle.write(
                "noauth\n"
                "refuse-eap\n"
                "refuse-pap\n"
                "refuse-chap\n"
                "refuse-mschap\n"
                f"name {VPN_USERNAME}\n"
                f"password {VPN_PASSWORD}\n"
                "noipdefault\n"
                "usepeerdns\n"
                "nodefaultroute\n"
                "novj\n"
            )

    def _dial(self) -> None:
        os.makedirs(os.path.dirname(self.L2TP_CONTROL), exist_ok=True)
        with open(self.L2TP_CONTROL, "w") as handle:
            handle.write(f"c {self.LAC_NAME}\n")

    def connect(self, server: VpnServer, protocol: Protocol, emit: Emit,
                workdir: str) -> dict:
        if sys.platform == "win32":
            return self._connect_windows(server, protocol, emit, workdir)
        started = time.monotonic()
        _log_emit(emit, f"[l2tp 1/5] Writing the L2TP/PPP definitions for "
                        f"{server.ip}:1701 (IKE PSK is the fixed 'vpn') ...")
        self._write_config(server)
        netutil.run(["pkill", "-f", "xl2tpd"], timeout=15)
        time.sleep(1)
        self._install_fixer(emit)
        # 1. Bring up the IPsec transport SA for UDP/1701.
        try:
            self._ipsec_up(server, emit)
        except Exception:
            self._remove_fixer()
            raise
        # 2. xl2tpd dials the LAC; its traffic is ESP encapsulated per policy.
        _log_emit(emit, f"[l2tp 2/5] Starting xl2tpd and dialing the LNS "
                        f"-> {server.ip}:1701 ...")
        log_path = os.path.join(workdir, "xl2tpd.log")
        xl2tpd_bin = netutil.which("xl2tpd") or "/usr/sbin/xl2tpd"
        with open(log_path, "w") as logfile:
            proc = subprocess.Popen([xl2tpd_bin, "-D",
                                     "-c", self.XL2TPD_CONF],
                                    stdout=logfile, stderr=logfile,
                                    stdin=subprocess.DEVNULL)
        time.sleep(1.5)
        try:
            self._dial()
        except OSError:
            pass

        deadline = time.time() + self.CONNECT_TIMEOUT
        iface = ""
        tick = 0
        while time.time() < deadline:
            iface = self._ppp_interface()
            if iface and netutil.interface_ipv4(iface):
                break
            if proc.poll() is not None:
                break
            tick += 1
            if tick % 5 == 0:                     # every ~10 s
                _log_emit(emit, f"[l2tp] waiting for the ppp handshake over "
                                f"ESP ({_since(started)} elapsed) ...")
            time.sleep(2)

        if not iface or not netutil.interface_ipv4(iface):
            proc.terminate()
            try:
                with open(log_path) as handle:
                    tail = "\n".join(handle.read().splitlines()[-10:])
            except OSError:
                tail = ""
            proc.terminate()
            self._ipsec_down()
            self._remove_fixer()
            raise BackendError("The L2TP/IPsec tunnel could not be established.\n" + tail)

        ip = netutil.interface_ipv4(iface)
        gateway = netutil.route_via(iface)
        if gateway:
            physical = self._physical_gateway()
            _log_emit(emit, f"[l2tp 3/5] {iface} is up with IP {ip}; installing "
                            f"the default route via {gateway} "
                            f"(old default via {physical or '?'} stays reachable)")
            netutil.install_tunnel_default(gateway, iface, server.ip, physical)
        _log_emit(emit, f"[l2tp 4/5] Connected in {_since(started)}. "
                        "The ESP-in-UDP checksum fixer stays on until disconnect.")
        return {
            "protocol": protocol.value,
            "server": f"{server.hostname} ({server.ip})",
            "interface": iface,
            "ip": ip or "",
            "gateway": gateway or "",
            "detail": {"port": 1701},
            "process": proc,
        }

    @staticmethod
    def _ppp_interface() -> str:
        for name in netutil.list_tunnel_interfaces():
            if name.startswith("ppp"):
                return name
        return ""

    @staticmethod
    def _physical_gateway() -> str:
        for gateway, device, _metric in netutil.default_routes():
            if not device.startswith(("tun", "ppp", "vpn_")):
                return gateway
        return ""

    def disconnect(self, info: dict, emit: Emit) -> None:
        if sys.platform == "win32":
            vpn_name = info.get("windows_vpn") or "VPNGate"
            _log_emit(emit, f"[l2tp] Disconnecting Windows RAS connection {vpn_name} ...")
            subprocess.run(["rasdial", vpn_name, "/DISCONNECT"],
                           capture_output=True, text=True, timeout=15)
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            f"Remove-VpnConnection -Name '{vpn_name}' -Force -ErrorAction SilentlyContinue"],
                           capture_output=True, text=True, timeout=15)
            _log_emit(emit, "Windows L2TP/IPsec disconnected.")
            return
        started = time.monotonic()
        proc = info.get("process")
        _log_emit(emit, "[l2tp] Hanging up the L2TP call (terminating xl2tpd) ...")
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:                      # noqa: BLE001
                try:
                    proc.kill()
                except Exception:                  # noqa: BLE001
                    pass
        iface = info.get("interface")
        gateway = info.get("gateway")
        if iface and gateway:
            _log_emit(emit, f"[l2tp] Removing the tunnel default route "
                            f"(via {gateway} dev {iface}) ...")
            netutil.remove_tunnel_default(gateway, iface)
        netutil.run(["pkill", "-f", "xl2tpd"], timeout=15)
        _log_emit(emit, "[l2tp] Tearing down the IPsec transport SA ...")
        self._ipsec_down()
        _log_emit(emit, "[l2tp] Stopping the ESP-in-UDP checksum fixer ...")
        self._remove_fixer()
        _log_emit(emit, f"L2TP/IPsec disconnected in {_since(started)}.")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

BACKENDS = {
    Protocol.OPENVPN: OpenVpnBackend(),
    Protocol.SSLVP: SoftEtherBackend(),
    Protocol.SSTP: SstpBackend(),
    Protocol.L2TP: L2tpBackend(),
}


def backend_for(protocol: Protocol) -> Backend:
    return BACKENDS[protocol]

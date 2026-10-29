"""Privileged worker process.

The worker is started by the GUI (as root) and then talks JSON lines over
stdin/stdout.  Keeping one long lived privileged process means the user is
only asked for authorisation once.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import traceback
from typing import Optional

from . import netutil
from .backends import BACKENDS, BackendError
from .config import STATE_DIR
from .models import Protocol, VpnServer

_emit_lock = threading.Lock()


def _emit(payload: dict) -> None:
    with _emit_lock:
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()


class Worker:
    def __init__(self) -> None:
        self.connection: Optional[dict] = None
        self.backend = None
        self.protocol: Optional[Protocol] = None
        if sys.platform != "win32" and hasattr(os, "geteuid") and os.geteuid() == 0:
            self.workdir = "/run/vpngate-client"
        else:
            self.workdir = os.path.join(STATE_DIR, "run")
        os.makedirs(self.workdir, exist_ok=True)
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

    def _monitor_loop(self) -> None:
        while True:
            time.sleep(1.5)
            conn = self.connection
            if conn is None:
                continue

            # 1. Process-based VPN check (OpenVPN / sstpc / xl2tpd)
            proc = conn.get("process")
            if proc is not None and proc.poll() is not None:
                code = proc.returncode
                self._log(f"Tunnel process terminated (exit code {code}).")
                _emit({"event": "error", "message": f"VPN tunnel process stopped (code {code})."})
                self.do_disconnect()
                continue

            # 2. Windows RAS VPN check (MS-SSTP / L2TP)
            win_vpn = conn.get("windows_vpn")
            if win_vpn and sys.platform == "win32":
                try:
                    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    res = subprocess.run(["rasdial"], capture_output=True, text=True,
                                         timeout=5, creationflags=creation_flags)
                    if win_vpn.lower() not in res.stdout.lower():
                        self._log(f"Windows VPN '{win_vpn}' connection dropped.")
                        _emit({"event": "error", "message": f"Windows VPN '{win_vpn}' connection dropped."})
                        self.do_disconnect()
                except Exception:
                    pass

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _server_from(data: dict) -> VpnServer:
        server = VpnServer()
        for key, value in (data or {}).items():
            if hasattr(server, key):
                setattr(server, key, value)
        return server

    def _log(self, kind: str, **kwargs) -> None:
        _emit({"event": "log", "text": str(kwargs.get("text", kind))})

    # -- commands ---------------------------------------------------------
    def handle(self, command: dict) -> None:
        action = command.get("cmd")
        if action == "connect":
            self.do_connect(command)
        elif action == "disconnect":
            self.do_disconnect()
        elif action == "status":
            self.do_status()
        elif action == "quit":
            self.do_disconnect()
            _emit({"event": "bye"})
            sys.exit(0)
        else:
            _emit({"event": "error", "message": f"Unknown command: {action}"})

    def do_connect(self, command: dict) -> None:
        if self.connection is not None:
            self.do_disconnect()
        try:
            protocol = Protocol(command.get("protocol", "openvpn"))
        except ValueError:
            _emit({"event": "error", "message": "Unsupported protocol."})
            return
        backend = BACKENDS.get(protocol)
        if backend is None:
            _emit({"event": "error", "message": "No backend for this protocol."})
            return
        if not backend.available():
            if sys.platform == "win32":
                if protocol == Protocol.OPENVPN:
                    msg = ("The OpenVPN client is not installed on this PC. "
                           "Please select MS-SSTP or L2TP/IPsec (built into Windows), "
                           "or install OpenVPN from https://openvpn.net")
                elif protocol == Protocol.SSLVP:
                    msg = ("The SSL-VPN (SoftEther) client is not installed on this PC. "
                           "Please select MS-SSTP or L2TP/IPsec (built into Windows), "
                           "or install SoftEther from https://www.vpngate.net")
                else:
                    msg = f"The {backend.name} client is not installed."
            else:
                msg = f"The {backend.name} client is not installed."
            _emit({"event": "error", "message": msg})
            return

        server = self._server_from(command.get("server", {}))
        _emit({"event": "state", "state": "connecting",
               "protocol": protocol.value,
               "message": f"Connecting to {server.hostname} ({server.ip}) "
                          f"using {backend.name} ..."})
        try:
            info = backend.connect(server, protocol, self._log, self.workdir)
        except BackendError as exc:
            _emit({"event": "error", "message": str(exc)})
            _emit({"event": "state", "state": "disconnected"})
            return
        except Exception as exc:                  # noqa: BLE001
            self._log(traceback.format_exc())
            _emit({"event": "error", "message": f"{type(exc).__name__}: {exc}"})
            _emit({"event": "state", "state": "disconnected"})
            return

        info["started_at"] = time.time()
        self.connection = info
        self.backend = backend
        self.protocol = protocol

        external = ""
        try:
            external = netutil.external_ip()
        except Exception:                          # noqa: BLE001
            pass
        info["external_ip"] = external
        _emit({"event": "connected", "info": _jsonable(info)})
        _emit({"event": "state", "state": "connected", "protocol": protocol.value,
               "message": f"Connected to {server.hostname} via {backend.name}"})

    def do_disconnect(self) -> None:
        if self.connection is None or self.backend is None:
            _emit({"event": "state", "state": "disconnected"})
            return
        conn = self.connection
        backend = self.backend
        self.connection = None
        self.backend = None
        self.protocol = None
        try:
            backend.disconnect(conn, self._log)
        except Exception as exc:                  # noqa: BLE001
            self._log(f"Error while disconnecting: {exc}")
        _emit({"event": "state", "state": "disconnected"})

    def do_status(self) -> None:
        if self.connection is None:
            _emit({"event": "state", "state": "disconnected"})
        else:
            _emit({"event": "connected", "info": _jsonable(self.connection)})
            _emit({"event": "state", "state": "connected"})


def _jsonable(info: dict) -> dict:
    out = {k: v for k, v in info.items() if k != "process"}
    return out


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("--check", "-c"):
        sys.exit(0)
    worker = Worker()
    uid = getattr(os, "getuid", lambda: 0)()
    _emit({"event": "ready", "uid": uid})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            command = json.loads(line)
        except json.JSONDecodeError:
            _emit({"event": "error", "message": "Malformed command."})
            continue
        try:
            worker.handle(command)
        except SystemExit:
            raise
        except Exception as exc:                  # noqa: BLE001
            self_log = traceback.format_exc()
            _emit({"event": "log", "text": self_log})
            _emit({"event": "error", "message": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    main()

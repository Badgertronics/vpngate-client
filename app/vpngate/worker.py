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
        self._connecting_backend = None
        self._connect_thread: Optional[threading.Thread] = None
        self._connect_lock = threading.Lock()
        self._is_connecting = False
        if sys.platform != "win32" and hasattr(os, "geteuid") and os.geteuid() == 0:
            self.workdir = "/run/vpngate-client"
        else:
            self.workdir = os.path.join(STATE_DIR, "run")
        os.makedirs(self.workdir, exist_ok=True)
        self._win_vpn_missed = 0
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

    def _monitor_loop(self) -> None:
        while True:
            time.sleep(1.5)
            conn = self.connection
            if conn is None:
                self._win_vpn_missed = 0
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
                        self._win_vpn_missed += 1
                        if self._win_vpn_missed >= 2:
                            self._win_vpn_missed = 0
                            self._log(f"Windows VPN '{win_vpn}' connection dropped.")
                            _emit({"event": "error", "message": f"Windows VPN '{win_vpn}' connection dropped."})
                            self.do_disconnect()
                    else:
                        self._win_vpn_missed = 0
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
            self.start_connect(command)
        elif action in ("disconnect", "abort"):
            self.do_abort_or_disconnect()
        elif action == "status":
            self.do_status()
        elif action == "quit":
            self.do_abort_or_disconnect()
            _emit({"event": "bye"})
            sys.exit(0)
        else:
            _emit({"event": "error", "message": f"Unknown command: {action}"})

    def do_abort_or_disconnect(self) -> None:
        with self._connect_lock:
            if self._is_connecting:
                self._log("Cancelling in-progress connection attempt ...")
                if self._connecting_backend is not None:
                    try:
                        self._connecting_backend.abort(self._log)
                    except Exception as exc:
                        self._log(f"Error aborting backend: {exc}")
                self._is_connecting = False
                self._connecting_backend = None
                _emit({"event": "state", "state": "disconnected", "message": "Connection cancelled."})
                return

        self.do_disconnect()

    def start_connect(self, command: dict) -> None:
        with self._connect_lock:
            if self._is_connecting:
                if self._connecting_backend is not None:
                    try:
                        self._connecting_backend.abort(self._log)
                    except Exception:
                        pass
                self._is_connecting = False

            if self.connection is not None:
                self.do_disconnect()

            try:
                protocol = Protocol(command.get("protocol", "openvpn"))
            except ValueError:
                _emit({"event": "error", "message": "Unsupported protocol."})
                _emit({"event": "state", "state": "disconnected"})
                return

            backend = BACKENDS.get(protocol)
            if backend is None:
                _emit({"event": "error", "message": "No backend for this protocol."})
                _emit({"event": "state", "state": "disconnected"})
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
                _emit({"event": "state", "state": "disconnected"})
                return

            server = self._server_from(command.get("server", {}))
            timeout = int(command.get("timeout") or 30)
            retries = int(command.get("retries") or 1)

            self._is_connecting = True
            self._connecting_backend = backend

            _emit({"event": "state", "state": "connecting",
                   "protocol": protocol.value,
                   "message": f"Connecting to {server.hostname} ({server.ip}) "
                              f"using {backend.name} ..."})

            thread = threading.Thread(
                target=self._connect_thread_worker,
                args=(backend, server, protocol, timeout, retries),
                daemon=True,
            )
            self._connect_thread = thread
            thread.start()

    def _connect_thread_worker(self, backend, server: VpnServer, protocol: Protocol,
                               timeout: int, retries: int) -> None:
        try:
            info = backend.connect(server, protocol, self._log, self.workdir,
                                   timeout=timeout, retries=retries)
        except BackendError as exc:
            with self._connect_lock:
                if not self._is_connecting or self._connecting_backend is not backend:
                    return
                self._is_connecting = False
                self._connecting_backend = None
            _emit({"event": "error", "message": str(exc)})
            _emit({"event": "state", "state": "disconnected"})
            return
        except Exception as exc:                  # noqa: BLE001
            with self._connect_lock:
                if not self._is_connecting or self._connecting_backend is not backend:
                    return
                self._is_connecting = False
                self._connecting_backend = None
            self._log(traceback.format_exc())
            _emit({"event": "error", "message": f"{type(exc).__name__}: {exc}"})
            _emit({"event": "state", "state": "disconnected"})
            return

        with self._connect_lock:
            if not self._is_connecting or self._connecting_backend is not backend:
                try:
                    backend.disconnect(info, self._log)
                except Exception:
                    pass
                return
            self._is_connecting = False
            self._connecting_backend = None

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
        self._win_vpn_missed = 0
        with self._connect_lock:
            conn = self.connection
            backend = self.backend
            self.connection = None
            self.backend = None
            self.protocol = None
        if conn is None or backend is None:
            _emit({"event": "state", "state": "disconnected"})
            return
        try:
            backend.disconnect(conn, self._log)
        except Exception as exc:                  # noqa: BLE001
            self._log(f"Error while disconnecting: {exc}")
        _emit({"event": "state", "state": "disconnected"})

    def do_status(self) -> None:
        with self._connect_lock:
            conn = self.connection
            is_connecting = self._is_connecting
        if is_connecting:
            _emit({"event": "state", "state": "connecting"})
        elif conn is None:
            _emit({"event": "state", "state": "disconnected"})
        else:
            _emit({"event": "connected", "info": _jsonable(conn)})
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

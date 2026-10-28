"""StatusNotifierItem-style tray icon for the VPN Gate client.

libayatana-appindicator is a GTK 3 binding and therefore must not be
loaded into this GTK 4 process (two major versions of Gtk in one process
is impossible with GI; it raises an ImportError like our earlier GTK4
namespace mix-up).  The icon therefore runs in a tiny dedicated helper
(:mod:`vpngate.tray_worker`) which is started by :func:`Tray.start` and
talks JSON lines over stdin/stdout:

    main app  ->  helper:  ``{"event": "state", "icon": ..., "tooltip": ...}``
                           ``{"event": "menu", "connect_enabled": ..., ...}``
                           EOF ends the helper quietly
    helper    ->  main app:  ``{"action": "show"|"connect"|"quit"}``

Anything going wrong in the helper is invisible to the GUI: if it can
not start (missing binding), or dies, the tray simply stays unusable and
the GUI keeps running.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from typing import Callable, Optional


class Tray:
    """Owns the helper process that draws the tray icon."""

    def __init__(self, icon_dir: str, on_action: Callable[[str], None]) -> None:
        self._icon_dir = icon_dir
        self._on_action = on_action
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._state = {"icon": "vpngate-tray-idle"}
        self._start()

    # -- helper lifecycle ----------------------------------------------------
    def _start(self) -> None:
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            worker_script = os.path.join(script_dir, "tray_worker.py")
            if not os.path.isfile(worker_script):
                return
            if getattr(sys, "frozen", False):
                import shutil
                python = shutil.which("python3") or shutil.which("python")
                if not python:
                    return
            else:
                python = sys.executable or "python3"
            args = [python, worker_script, "--icon-dir", self._icon_dir]
            proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
                start_new_session=False)
        except OSError as exc:                         # python gone?!
            print(f"tray helper failed to start: {exc}", file=sys.stderr)
            return
        self._proc = proc
        threading.Thread(target=self._read_actions, daemon=True).start()
        self._send(self._state)

    def _send(self, payload: dict) -> None:
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return
            try:
                proc.stdin.write(json.dumps(payload) + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                self._proc = None

    def _read_actions(self) -> None:
        assert self._proc is not None
        for line in self._proc.stdout:                 # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            action = event.get("action")
            if action:
                try:
                    self._on_action(action)
                except Exception:                       # noqa: BLE001
                    pass
        self._proc = None

    # -- public API ----------------------------------------------------------
    def set_state(self, status: str) -> None:
        """Map an app state to an icon name + tooltip."""
        mapping = {
            "idle": ("vpngate-tray-idle", "VPN Gate Client - disconnected"),
            "authorizing": ("vpngate-tray-connecting",
                            "VPN Gate Client - requesting privileges ..."),
            "connecting": ("vpngate-tray-connecting", "VPN Gate Client - connecting ..."),
            "connected": ("vpngate-tray-connected", "VPN Gate Client - connected"),
            "error": ("vpngate-tray-error", "VPN Gate Client - error"),
        }
        icon, text = mapping.get(status, mapping["idle"])
        self._state = {"icon": icon, "tooltip": text}
        self._send(self._state)

    def set_menu(self, **flags: bool) -> None:
        self._send({"event": "menu", **flags})

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()                     # EOF ends the helper
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=3)
        except Exception:                              # noqa: BLE001
            try:
                proc.terminate()
            except Exception:                          # noqa: BLE001
                pass

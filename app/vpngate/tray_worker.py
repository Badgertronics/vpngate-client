#!/usr/bin/env python3
"""Tray icon helper process (GTK 3 + libayatana-appindicator).

Runs as a child of the GTK 4 GUI exactly so that the Gtk 3 namespace can
be used here in isolation.  Reads one JSON line per state change on
stdin, updates the indicator icon/menu, and writes one JSON line for
each menu action on stdout.  Exits on EOF or ``{"action": "quit"}``.
"""
from __future__ import annotations

import json
import signal
import sys


def _fail(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(1)


def main() -> int:                                     # noqa: C901
    if len(sys.argv) >= 3 and sys.argv[1] == "--icon-dir":
        icon_dir = sys.argv[2]
    else:
        _fail("usage: tray_worker.py --icon-dir DIR")

    import gi
    try:
        gi.require_version("Gtk", "3.0")
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3, GLib, Gtk
    except (ImportError, ValueError) as exc:
        _fail(f"tray bindings unavailable: {exc}")

    indicator = AppIndicator3.Indicator.new(
        "com.vpngate.client", "vpngate-tray-idle",
        AppIndicator3.IndicatorCategory.APPLICATION_STATUS)
    indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
    import os                                           # noqa: E402  (late)
    if os.path.isdir(icon_dir):
        indicator.set_icon_theme_path(icon_dir)

    menu = Gtk.Menu()
    flags = {"show": True, "connect_enabled": False, "disconnect_enabled": False}

    def build_menu() -> None:
        for child in menu.get_children():
            menu.remove(child)
        item = Gtk.MenuItem(label="Show VPN Gate")
        item.connect("activate", lambda *_: _emit({"action": "show"}))
        menu.append(item)
        connect = Gtk.MenuItem(label="Connect (recommended)")
        connect.set_sensitive(flags.get("connect_enabled", False))
        connect.connect("activate", lambda *_: _emit({"action": "connect"}))
        menu.append(connect)
        disconnect = Gtk.MenuItem(label="Disconnect")
        disconnect.set_sensitive(flags.get("disconnect_enabled", False))
        disconnect.connect("activate", lambda *_: _emit({"action": "disconnect"}))
        menu.append(disconnect)
        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda *_: _emit({"action": "quit"}))
        menu.append(quit_item)
        menu.show_all()
        indicator.set_menu(menu)

    out = sys.stdout

    def _emit(payload: dict) -> None:
        try:
            out.write(json.dumps(payload) + "\n")
            out.flush()
        except Exception:                              # noqa: BLE001
            pass

    build_menu()

    def handle_stdin(_src, condition):
        data = _src.readline()
        if not data:
            GLib.idle_add(Gtk.main_quit)
            return GLib.SOURCE_REMOVE
        try:
            payload = json.loads(data.strip())
        except ValueError:
            return GLib.SOURCE_CONTINUE
        if payload.get("event") == "state":
            icon = payload.get("icon") or "vpngate-tray-idle"
            indicator.set_icon_full(icon, "icon")
            tooltip = payload.get("tooltip") or ""
            if tooltip:
                indicator.set_title(tooltip)           # shown by hover hints
        elif payload.get("event") == "menu":
            flags.update({k: bool(v) for k, v in payload.items()
                          if k in ("show", "connect_enabled",
                                   "disconnect_enabled")})
            build_menu()
        elif payload.get("cmd") == "quit":
            GLib.idle_add(Gtk.main_quit)
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    channel = GLib.IOChannel.unix_new(sys.stdin.fileno())
    GLib.io_add_watch(channel, GLib.IO_IN | GLib.IO_HUP, handle_stdin)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    build_menu()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

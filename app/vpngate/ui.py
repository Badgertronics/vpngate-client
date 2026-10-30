"""Premium VPN Gate client frontend, written from scratch on GTK4 + libadwaita.

Design goals: hero connection panel with the animated status ring, server
card list with filters and live sorting, details side panel, terminal
style log drawer (Adw.BottomSheet), toasts and banners for every state,
settings via Adw.PreferencesDialog and a helper-process tray icon.

All IO (mirror fetches, worker JSON, cache files) happens on threads and
is marshalled back with ``GLib.idle_add``; the GUI never blocks.  The
root worker protocol in :mod:`vpngate.worker` / :mod:`vpngate.privilege`
is used unchanged.
"""
from __future__ import annotations

import faulthandler
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import traceback
from gettext import gettext as _
from typing import Callable, Dict, List, Optional

if sys.platform == "win32":
    os.environ.setdefault("GSK_RENDERER", "cairo")
    os.environ.setdefault("GSETTINGS_BACKEND", "memory")
    os.environ.setdefault("GIO_USE_VFS", "local")
else:
    os.environ.setdefault("GSK_RENDERER", "gl")

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gdk, Gio, Gtk, Pango  # noqa: E402

from . import fetcher, privilege                      # noqa: E402
from .config import (APP_NAME, DEFAULT_CONNECT_RETRIES, DEFAULT_CONNECT_TIMEOUT,  # noqa: E402
                     DEFAULT_CONNECTIVITY_TARGETS, DEFAULT_MIRRORS,
                     DEFAULT_PROBE_TIMEOUT, DEFAULT_WATCHDOG_INTERVAL,
                     DEVELOPER_NAME, DONATE_BEP20, DONATE_BITCOIN,
                     DONATE_ETHEREUM, GITHUB_URL, STATE_DIR, VERSION)
from .models import PROTOCOL_ORDER, Protocol, VpnServer  # noqa: E402
from .tray import Tray                                # noqa: E402
from .widgets import (SpeedBar, StatusRing, Segmented, apply_theme_css,
                      flag_emoji, flag_text, flag_widget, ping_chip,
                      ping_level, protocol_chip, protocol_name,
                      skeleton_rows, stat_chip)  # noqa: E402

APP_ID = "com.vpngate.client"
APP_LOG = "VPN Gate Client"
_QUICK_COUNTRIES = ("JP", "KR", "TH")
DEFAULT_PROTOCOL = "sstp" if sys.platform == "win32" else "sslvp"

if getattr(sys, "frozen", False):
    ROOT_DIR = os.path.dirname(sys.executable)
else:
    ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICONS_DIR = os.path.join(ROOT_DIR, "icons", "hicolor", "scalable", "apps")


def _make_wrap_box(child_spacing: int = 6, line_spacing: int = 6, **kwargs) -> Gtk.Widget:
    """Create an Adw.WrapBox if Libadwaita >= 1.6, or fallback to Gtk.Box."""
    wrap_cls = getattr(Adw, "WrapBox", None)
    if wrap_cls is not None:
        return wrap_cls(child_spacing=child_spacing, line_spacing=line_spacing, **kwargs)
    return Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=child_spacing, **kwargs)


def _make_spin_row(title: str, subtitle: str, min_v: float, max_v: float, step: float, current: float, digits: int = 0) -> Gtk.Widget:
    """Create an Adw.SpinRow (Libadwaita >= 1.2) or fallback to ActionRow with Gtk.SpinButton."""
    if hasattr(Adw, "SpinRow"):
        row = Adw.SpinRow.new_with_range(min_v, max_v, step)
        row.set_title(title)
        row.set_subtitle(subtitle)
        row.set_digits(digits)
        row.set_value(current)
        return row
    row = Adw.ActionRow(title=title, subtitle=subtitle)
    adj = Gtk.Adjustment(value=current, lower=min_v, upper=max_v, step_increment=step, page_increment=step * 2)
    spin = Gtk.SpinButton(adjustment=adj, digits=digits, valign=Gtk.Align.CENTER)
    row.add_suffix(spin)
    row.get_value = lambda: spin.get_value()
    row.set_value = lambda v: spin.set_value(v)
    row.connect = lambda sig, cb: spin.connect("value-changed", lambda s: cb(row, None))
    return row


class _SheetFallback(Gtk.Box):
    """Container with a stretchable bottom log drawer."""

    def __init__(self, content: Gtk.Widget, initial_height: int = 240) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._content = content
        self._content.set_vexpand(True)
        self._content.set_hexpand(True)
        self.append(content)
        self._revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_UP,
            reveal_child=False,
            visible=False,
        )
        self.append(self._revealer)
        self._log_height = max(80, int(initial_height))
        self._panel: Optional[Gtk.Widget] = None
        self._handle: Optional[Gtk.Widget] = None

        class _Props:
            def __init__(inner):
                inner.open = False
                inner.sheet = None
                inner.content = content
                inner.full_width = True
                inner.modal = False
                inner.show_drag_handle = False

            def __setattr__(inner, name, value):
                super(_Props, inner).__setattr__(name, value)
                if name == "open":
                    is_open = bool(value)
                    if is_open:
                        self._revealer.set_visible(True)
                        if self._panel is not None:
                            self._panel.props.height_request = self._log_height
                    self._revealer.set_reveal_child(is_open)
                elif name == "sheet" and value is not None:
                    self._attach_sheet(value)

        self.props = _Props()
        self._revealer.connect("notify::child-revealed", self._on_revealer_transition_done)

    def _attach_sheet(self, panel: Gtk.Widget) -> None:
        self._panel = panel
        panel.set_size_request(-1, 80)
        panel.props.height_request = self._log_height

        # Drag handle at the top of the log panel
        handle = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        handle.add_css_class("log-drag-handle")
        handle.props.height_request = 10
        try:
            handle.set_cursor_from_name("row-resize")
        except Exception:
            pass

        grip = Gtk.Box()
        grip.props.width_request = 38
        grip.props.height_request = 3
        grip.props.halign = Gtk.Align.CENTER
        grip.props.valign = Gtk.Align.CENTER
        grip.add_css_class("log-drag-grip")
        handle.append(grip)

        drag = Gtk.GestureDrag()
        initial_h = [self._log_height]

        def on_drag_begin(_g, _x, _y):
            curr = panel.get_height()
            initial_h[0] = curr if curr > 0 else self._log_height

        def on_drag_update(_g, _ox, oy):
            # Dragging upwards (negative oy) increases panel height
            root = self.get_root()
            total_h = root.get_height() if root and hasattr(root, "get_height") else 700
            # Leave at least 320px for the header bar and splitview content
            max_h = max(100, min(int(total_h * 0.55), total_h - 320))
            new_h = int(max(80, min(max_h, initial_h[0] - oy)))
            self._log_height = new_h
            panel.props.height_request = new_h

        def on_drag_end(_g, _ox, _oy):
            root = self.get_root()
            if root is not None and hasattr(root, "settings"):
                root.settings["log_height"] = self._log_height
                save_settings(root.settings)

        drag.connect("drag-begin", on_drag_begin)
        drag.connect("drag-update", on_drag_update)
        drag.connect("drag-end", on_drag_end)
        handle.add_controller(drag)
        self._handle = handle

        panel.prepend(handle)
        self._revealer.set_child(panel)
        if self.props.open:
            self._revealer.set_visible(True)
            self._revealer.set_reveal_child(True)

    def _on_revealer_transition_done(self, revealer, _param) -> None:
        if not revealer.get_child_revealed() and not self.props.open:
            revealer.set_visible(False)


# ---------------------------------------------------------------------------
# Settings / crash capture
# ---------------------------------------------------------------------------

def load_settings() -> dict:
    try:
        with open(os.path.join(STATE_DIR, "settings.json")) as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        with open(os.path.join(STATE_DIR, "settings.json"), "w") as handle:
            json.dump(settings, handle, indent=2)
    except OSError:
        pass


def _install_crash_capture() -> None:
    """Capture native and Python level crashes into state/gui_crash.log.

    GNOME hides the stderr of a GUI process, so a segfault (which has
    happened inside Pango) or an unhandled exception would otherwise
    vanish; faulthandler captures the C level, sys.excepthook the Python
    level.
    """
    try:
        crash_log = open(os.path.join(STATE_DIR, "gui_crash.log"), "a",
                         buffering=1, encoding="utf-8", errors="replace")
    except OSError:
        return
    try:
        faulthandler.enable(file=crash_log)
    except (AttributeError, OSError):
        pass
    previous = sys.excepthook

    def _hook(kind, value, tb) -> None:                # noqa: ANN001
        try:
            crash_log.write(f"\n=== unhandled {kind.__name__} ===\n")
            traceback.print_exception(kind, value, tb, file=crash_log)
            traceback.print_exception(kind, value, tb)
            crash_log.flush()
        except Exception:                              # noqa: BLE001
            pass
        if previous is not None:
            previous(kind, value, tb)

    sys.excepthook = _hook


def _server_from_dict(item: dict) -> VpnServer:
    server = VpnServer()
    for key, value in (item or {}).items():
        if hasattr(server, key):
            setattr(server, key, value)
    return server


# ---------------------------------------------------------------------------
# Sorting / recommendations
# ---------------------------------------------------------------------------

_SORT_MODES = (
    "recommended",
    "speed",
    "ping",
    "sessions_asc",
    "sessions_desc",
    "uptime",
    "traffic",
    "users",
    "hostname",
    "country",
)


def sort_servers(servers: List[VpnServer], mode: str) -> List[VpnServer]:
    out = list(servers)
    if mode == "speed":
        out.sort(key=lambda s: (-s.speed_mbps, s.ping_ms or 0))
    elif mode == "ping":
        out.sort(key=lambda s: (s.ping_ms or 99999, -s.speed_mbps))
    elif mode == "sessions_asc":
        out.sort(key=lambda s: (s.sessions, -s.speed_mbps))
    elif mode == "sessions_desc":
        out.sort(key=lambda s: (-s.sessions, -s.speed_mbps))
    elif mode == "uptime":
        out.sort(key=lambda s: (-getattr(s, "uptime_seconds", 0), -s.speed_mbps))
    elif mode == "traffic":
        out.sort(key=lambda s: (-getattr(s, "total_traffic_bytes", 0), -s.speed_mbps))
    elif mode == "users":
        out.sort(key=lambda s: (-s.total_users, -s.speed_mbps))
    elif mode == "hostname":
        out.sort(key=lambda s: ((s.hostname or s.ip).lower(), s.country))
    elif mode == "country":
        out.sort(key=lambda s: (s.country, s.country_code))
    else:                                              # "recommended"
        out.sort(key=lambda s: (-s.score, -s.speed_mbps, s.ping_ms or 0))
    return out


def recommended_server(servers: List[VpnServer]) -> Optional[VpnServer]:
    """Best pick right now: SSTP on Windows, SSL-VPN on Linux, JP/KR/TH first, then score."""
    target_proto = Protocol.SSTP if sys.platform == "win32" else Protocol.SSLVP
    pool_all = [s for s in servers if s.supports(target_proto)]
    pool = [s for s in pool_all
            if (s.country_code or "").upper() in _QUICK_COUNTRIES]
    pool = pool or pool_all or list(servers)
    if not pool:
        return None
    return max(pool, key=lambda s: (s.score, s.speed_mbps, -s.ping_ms))


def quick_stack(servers: List[VpnServer], limit: int = 3) -> List[VpnServer]:
    """A short list of one-click servers for the 'recommended' strip."""
    target_proto = Protocol.SSTP if sys.platform == "win32" else Protocol.SSLVP
    picks: List[VpnServer] = []
    preferred = [s for s in servers
                 if (s.country_code or "").upper() in _QUICK_COUNTRIES
                 and s.supports(target_proto)]
    for server in sorted(preferred, key=lambda s: -s.score):
        if len(picks) >= limit:
            break
        if all(p.ip != server.ip for p in picks):
            picks.append(server)
    if len(picks) < limit:
        for server in sort_servers(
            [s for s in servers if s.supports(target_proto)], "recommended"
        ):
            if len(picks) >= limit:
                break
            if all(p.ip != server.ip for p in picks):
                picks.append(server)
    if len(picks) < limit:
        for server in sort_servers(servers, "recommended"):
            if len(picks) >= limit:
                break
            if all(p.ip != server.ip for p in picks):
                picks.append(server)
    return picks[:limit]


def _clean_probe_err(err: Exception | str) -> str:
    if isinstance(err, Exception) and hasattr(err, "reason"):
        s = str(err.reason).strip()
    else:
        s = str(err or "").strip()
    m = re.search(r"urlopen error (.*)", s, flags=re.IGNORECASE)
    if m:
        s = m.group(1).strip()
    s = s.replace("<", "").replace(">", "").strip()
    s_lower = s.lower()
    if "timed out" in s_lower or "timeout" in s_lower:
        return "Connection timed out"
    if "connection refused" in s_lower:
        return "Connection refused"
    if "name or service not known" in s_lower or "nodename nor servname" in s_lower or "getaddrinfo failed" in s_lower:
        return "DNS lookup failed"
    if "certificate" in s_lower or "ssl" in s_lower or "handshake" in s_lower:
        return "SSL/TLS handshake failed"
    if "network is unreachable" in s_lower or "host is unreachable" in s_lower:
        return "Network unreachable"
    if "connection reset" in s_lower:
        return "Connection reset by peer"
    return s or "Connection failed"


def probe_target_detailed(target: str, timeout: float = 3.5) -> tuple[bool, int, str]:
    """Probe a connectivity target and return (is_reachable, rtt_ms, detail_str)."""
    target = (target or "").strip()
    if not target:
        return False, 0, "Address is empty"
    import ssl
    import urllib.error
    import urllib.request

    t0 = time.time()

    # 1. Explicit HTTP / HTTPS URL
    if target.startswith("http://") or target.startswith("https://"):
        try:
            req = urllib.request.Request(target, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VPN-Gate-Tester/1.0"})
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                ms = max(1, int((time.time() - t0) * 1000))
                status = resp.status
                reason = getattr(resp, "reason", "OK")
                return status < 500, ms, f"HTTP {status} {reason}"
        except urllib.error.HTTPError as ex:
            ms = max(1, int((time.time() - t0) * 1000))
            return ex.code < 500, ms, f"HTTP {ex.code} {ex.reason}"
        except urllib.error.URLError as ex:
            ms = max(1, int((time.time() - t0) * 1000))
            return False, ms, _clean_probe_err(ex.reason)
        except Exception as ex:
            ms = max(1, int((time.time() - t0) * 1000))
            return False, ms, _clean_probe_err(ex)

    # 2. host:port specified
    if ":" in target and not target.startswith("["):
        try:
            host, port_s = target.rsplit(":", 1)
            port = int(port_s)
            with socket.create_connection((host, port), timeout=timeout):
                ms = max(1, int((time.time() - t0) * 1000))
                return True, ms, f"TCP port {port} open"
        except socket.timeout:
            ms = max(1, int((time.time() - t0) * 1000))
            return False, ms, f"TCP connection to port {port} timed out"
        except Exception as ex:
            ms = max(1, int((time.time() - t0) * 1000))
            return False, ms, _clean_probe_err(ex)

    # 3. Numeric IPv4 address (e.g. 1.1.1.1, 8.8.8.8)
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", target):
        for test_port in (53, 443, 80):
            try:
                with socket.create_connection((target, test_port), timeout=timeout):
                    ms = max(1, int((time.time() - t0) * 1000))
                    return True, ms, f"TCP port {test_port} connected"
            except Exception:
                pass
        ms = max(1, int((time.time() - t0) * 1000))
        return False, ms, "Ports 53/443/80 unreachable"

    # 4. Hostname (e.g. youtube.com, google.com, cloudflare.com)
    # Perform strict HTTPS probe with SSL verification so DPI/censorship proxies cannot fake it
    try:
        url = f"https://{target}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VPN-Gate-Tester/1.0"})
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            ms = max(1, int((time.time() - t0) * 1000))
            status = resp.status
            reason = getattr(resp, "reason", "OK")
            return status < 500, ms, f"HTTPS {status} {reason} (TLS verified)"
    except urllib.error.HTTPError as ex:
        ms = max(1, int((time.time() - t0) * 1000))
        return ex.code < 500, ms, f"HTTPS {ex.code} {ex.reason}"
    except Exception as ex_https:
        err_msg = _clean_probe_err(ex_https)
        try:
            url = f"http://{target}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VPN-Gate-Tester/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ms = max(1, int((time.time() - t0) * 1000))
                status = resp.status
                reason = getattr(resp, "reason", "OK")
                return status < 500, ms, f"HTTP {status} {reason}"
        except urllib.error.HTTPError as ex:
            ms = max(1, int((time.time() - t0) * 1000))
            return ex.code < 500, ms, f"HTTP {ex.code} {ex.reason}"
        except Exception as ex_http:
            ms = max(1, int((time.time() - t0) * 1000))
            return False, ms, _clean_probe_err(ex_http) or err_msg


def probe_target(target: str, timeout: float = 3.0) -> bool:
    ok, _ms, _details = probe_target_detailed(target, timeout=timeout)
    return ok


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class VpnGateApp(Adw.Application):
    """Adw.Application shell: owns actions, accelerators and the window."""

    def __init__(self) -> None:
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.win: Optional[VpnGateWindow] = None
        self.settings: dict = load_settings()
        self._install_actions()

    def _install_actions(self) -> None:
        for name, accels in (
            ("refresh", ["<Primary>R"]),
            ("focus-search", ["<Primary>F"]),
            ("log-drawer", ["<Primary>L"]),
            ("connect-quick", ["<Primary>Return"]),
            ("escape", ["Escape"]),
            ("settings", ["<Primary>comma"]),
            ("about", []),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", self._dispatch)
            self.add_action(action)
            self.set_accels_for_action(f"app.{name}", accels)

    def _dispatch(self, action: Gio.SimpleAction, _param) -> None:
        win = self.win
        if win is None:
            return
        handler = getattr(win, f"act_{action.get_name().replace('-', '_')}",
                          None)
        if handler is not None:
            handler()


    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        os.makedirs(STATE_DIR, exist_ok=True)
        _install_crash_capture()

    def do_activate(self) -> None:
        display = Gdk.Display.get_default()
        if display is not None:
            apply_theme_css()
            theme = Gtk.IconTheme.get_for_display(display)
            for candidate in (
                ICONS_DIR,
                os.path.join(ROOT_DIR, "icons"),
                os.path.join(ROOT_DIR, "icons", "hicolor", "scalable", "apps"),
                os.path.join(os.path.dirname(__file__), "..", "icons", "hicolor", "scalable", "apps"),
                os.path.join(os.path.dirname(__file__), "..", "icons"),
            ):
                if os.path.isdir(candidate):
                    theme.add_search_path(candidate)
                    theme.add_search_path(os.path.dirname(candidate))
        self.win = VpnGateWindow(self)
        self.win.present()
        if self.settings.get("tray", True):
            self.win.setup_tray()


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

class VpnGateWindow(Adw.ApplicationWindow):
    """Hero panel, server browser, details sidebar and the log drawer."""

    def __init__(self, app: VpnGateApp) -> None:
        super().__init__(application=app)
        self.app = app
        self.settings = app.settings

        self.servers: List[VpnServer] = []
        self.mirrors: List[str] = (self.settings.get("mirrors")
                                   or list(DEFAULT_MIRRORS))
        self.connectivity_targets: List[str] = (
            self.settings.get("connectivity_targets")
            or list(DEFAULT_CONNECTIVITY_TARGETS)
        )
        self._watchdog_in_flight = False
        self._watchdog_last_check = 0.0
        self._watchdog_failures = 0
        self.state = "idle"          # idle/authorizing/connecting/connected/error
        self.connected_info: Optional[dict] = None
        self.selected: Optional[VpnServer] = None
        self.recommended: Optional[VpnServer] = None
        self.worker = None
        self.worker_method = ""
        self._sudo_password: Optional[str] = None
        self._refreshing = False
        self._pending: Optional[tuple] = None        # (server, Protocol) for spawn
        self._aborting = False                       # Stop pressed mid-dial
        self._auth_dialog: Optional[Adw.AlertDialog] = None
        self._dot_phase = 0                          # animated "connecting." dots
        self._protocol_choice: Dict[str, str] = {}
        self._row_for: Dict[int, VpnServer] = {}
        self._rows: Dict[int, Gtk.ListBoxRow] = {}
        self._row_seq = 0
        self._sort = "recommended"
        self._session_started = 0.0
        self._search_id: Optional[int] = None
        self.tray: Optional[Tray] = None

        disp = Gdk.Display.get_default()
        monitors = disp.get_monitors() if disp else None
        if monitors and monitors.get_n_items() > 0:
            geom = monitors.get_item(0).get_geometry()
            self.set_default_size(geom.width, geom.height)
        else:
            self.set_default_size(1024, 768)
        self.maximize()
        # Floor wide enough for content and hero panel
        self.set_size_request(600, 480)
        self.set_icon_name(APP_ID)

        self._build_ui()
        self._install_breakpoints()
        self._apply_saved_appearance()
        self._open_sheet()
        self._watch_network()
        GLib.timeout_add_seconds(1, self._tick)
        GLib.idle_add(self._load_cached)

    # ==================================================================
    # Widget tree
    # ==================================================================

    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()
        self.toolbar = toolbar
        toolbar.add_top_bar(self._build_header())
        self.split = Adw.OverlaySplitView(sidebar_position=Gtk.PackType.END)
        self.split.set_vexpand(True)
        self.split.set_hexpand(True)
        self.split.props.min_sidebar_width = 320
        self.split.props.max_sidebar_width = 360
        self.split.set_sidebar(self._build_details())
        self.split.set_content(self._build_clamped_content())
        self.sheet = self._build_sheet()      # assigns content = self.split
        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(self.sheet)
        toolbar.set_content(self.toast_overlay)
        self.set_content(toolbar)

    def _build_header(self) -> Adw.HeaderBar:
        self.title_widget = Adw.WindowTitle(
            title=APP_LOG, subtitle=_("connecting to the relay directory …"))
        bar = Adw.HeaderBar()
        bar.props.title_widget = self.title_widget

        self.refresh_btn = Gtk.Button(
            icon_name="view-refresh-symbolic",
            tooltip_text=_("Download the daily server list (Ctrl+R)"))
        self.refresh_btn.connect("clicked", lambda *_: self.act_refresh())
        bar.pack_start(self.refresh_btn)
        # Loading ring shown while a connection attempt is in flight.
        self.header_spinner = Gtk.Spinner(spinning=True, margin_start=4)
        self.header_spinner.set_valign(Gtk.Align.CENTER)
        self.header_spinner.set_visible(False)
        bar.pack_start(self.header_spinner)

        self.sheet_btn = Gtk.ToggleButton(
            icon_name="terminal-symbolic",
            tooltip_text=_("Connection log (Ctrl+L)"))
        self.sheet_btn.connect("toggled", self._on_sheet_btn)
        settings_btn = Gtk.Button(icon_name="preferences-system-symbolic",
                                  tooltip_text=_("Settings (Ctrl+,)"))
        settings_btn.connect("clicked", lambda *_: self.act_settings())
        about_btn = Gtk.Button(icon_name="help-about-symbolic",
                               tooltip_text=_("About"))
        about_btn.connect("clicked", lambda *_: self.act_about())
        bar.pack_end(settings_btn)
        bar.pack_end(about_btn)
        bar.pack_end(self.sheet_btn)
        return bar

    def _build_clamped_content(self) -> Gtk.Widget:
        clamp = Adw.Clamp()
        clamp.props.maximum_size = 780
        clamp.props.tightening_threshold = 560
        clamp.set_vexpand(True)
        clamp.set_hexpand(True)
        clip_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                           margin_start=12, margin_end=12, margin_top=8,
                           margin_bottom=10, spacing=10)
        clip_box.set_vexpand(True)
        clip_box.set_hexpand(True)
        clamp.set_child(clip_box)
        self._fill_content(clip_box)
        return clamp

    def _fill_content(self, clip_box: Gtk.Box) -> None:
        self.banners = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.offline_banner = self._banner(_("No network connection"))
        self.fallback_banner = self._banner(
            _("All mirror sites were unreachable — using a snapshot from "
              "the project repository or the CDN (a little older, but "
              "usable)."))
        self.auth_banner = self._banner(
            _("Administrator privileges are required to start the helper."),
            button_label=_("Retry"))
        self.auth_banner.connect("button-clicked",
                                 lambda *_: self._retry_authorize())
        for banner in (self.offline_banner, self.fallback_banner,
                       self.auth_banner):
            self.banners.append(banner)
        clip_box.append(self.banners)
        clip_box.append(self._build_hero())
        self.quick_strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                                   spacing=8, visible=False)
        clip_box.append(self.quick_strip)
        clip_box.append(self._build_filters())
        clip_box.append(self._build_list_stack())

    def _banner(self, text: str, button_label: Optional[str] = None) -> Adw.Banner:
        banner = Adw.Banner()
        banner.props.title = text
        if button_label:
            banner.props.button_label = button_label
        banner.props.revealed = False
        return banner

    def _build_hero(self) -> Gtk.Widget:
        hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16,
                       css_classes=["hero", "hero-boxed", "hero-grad"])
        self.hero_ring = StatusRing()
        hero.append(self.hero_ring)

        text_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7,
                           valign=Gtk.Align.CENTER, hexpand=True)
        self.hero_title = Gtk.Label(css_classes=["hero-title"],
                                    label=_("Disconnected"),
                                    ellipsize=Pango.EllipsizeMode.END)
        self.hero_title.props.xalign = 0.0
        self.hero_sub = Gtk.Label(wrap=False, lines=1, ellipsize=Pango.EllipsizeMode.END,
                                  css_classes=["hero-sub", "dim-x"])
        self.hero_sub.props.max_width_chars = 48
        # WrapBox: if the five tunnel chips exceed the row they fall onto a
        # second line instead of pushing the hero past the window width.
        chips = _make_wrap_box(child_spacing=6, line_spacing=6)
        self.chip_server = stat_chip("")
        self.chip_tunnel = stat_chip("")
        self.chip_public = stat_chip("")
        self.chip_iface = stat_chip("")
        self.chip_uptime = stat_chip("")
        self.chip_server.props.max_width_chars = 34
        for chip in (self.chip_tunnel, self.chip_public,
                     self.chip_iface, self.chip_uptime):
            chip.props.max_width_chars = 24
        for chip in (self.chip_server, self.chip_tunnel, self.chip_public,
                     self.chip_iface, self.chip_uptime):
            chip.set_visible(False)
            chips.append(chip)
        self.hero_sub.props.xalign = 0.0
        self.hero_sub.set_text(_("Choose a server below, or connect to the "
                                 "recommended relay."))
        text_col.append(self.hero_title)
        text_col.append(self.hero_sub)
        text_col.append(chips)
        hero.append(text_col)

        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                          valign=Gtk.Align.CENTER, halign=Gtk.Align.END)
        self.connect_btn = Gtk.Button(
            label=_("Connect"), css_classes=["vpn-connect", "suggested-action",
                                             "pill"],
            tooltip_text=_("Connect now — your selection, or the best "
                           "available relay (Enter)"))
        self.connect_btn.connect("clicked", self._on_connect_clicked)
        self.connect_btn.set_sensitive(False)
        buttons.append(self.connect_btn)
        self.hero_hint = Gtk.Label(xalign=1, wrap=True,
                                   css_classes=["dim"],
                                   label=_("recommended servers are one "
                                           "click away"))
        buttons.append(self.hero_hint)
        hero.append(buttons)
        return hero

    def _build_quick_strip(self, servers: List[VpnServer]) -> None:
        child = self.quick_strip.get_first_child()
        while child is not None:
            next_sibling = child.get_next_sibling()
            self.quick_strip.remove(child)
            child = next_sibling
        picks = quick_stack(servers)
        if not picks:
            self.quick_strip.set_visible(False)
            return
        self.quick_strip.set_visible(True)
        note = Gtk.Label(label=_("Recommended"),
                         css_classes=["chip", "chip-teal"],
                         valign=Gtk.Align.CENTER,
                         tooltip_text=_("SSL-VPN relays in Japan, Korea or "
                                        "Thailand are picked first, then "
                                        "the highest score"))
        self.quick_strip.append(note)
        proto_label = "MS-SSTP" if sys.platform == "win32" else "SSL-VPN"
        for server in picks:
            button = Gtk.Button(
                css_classes=["chip", "chip-teal"],
                tooltip_text=_("Connect now to {host} via {proto}").format(
                    host=server.hostname or server.ip, proto=proto_label))
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            line.append(flag_widget(server.country_code))
            name = (server.hostname or server.ip or "?").split(".")[0]
            line.append(Gtk.Label(label=name, valign=Gtk.Align.CENTER,
                                  css_classes=["dim-x"],
                                  ellipsize=Pango.EllipsizeMode.END,
                                  max_width_chars=18))
            button.set_child(line)
            button.connect("clicked", self._on_quick_connect, server)
            self.quick_strip.append(button)

    def _build_filters(self) -> Gtk.Widget:
        self.filters = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        row_top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.search = Gtk.SearchEntry(
            hexpand=True, halign=Gtk.Align.FILL, max_width_chars=36,
            placeholder_text=_("Search country, host or IP … (Ctrl+F)"))
        self.search.connect("search-changed", self._on_search_changed)
        row_top.append(self.search)

        self.country_model = Gtk.StringList.new([_("All countries")])
        self.country_dd = Gtk.DropDown.new(self.country_model, None)
        self.country_dd.connect("notify::selected-item",
                                lambda *_: self._apply_filters())
        row_top.append(self.country_dd)

        sort_items = [
            _("Recommended"),
            _("Fastest speed"),
            _("Lowest ping (latency)"),
            _("Lowest sessions"),
            _("Highest sessions"),
            _("Longest uptime"),
            _("Cumulative transfers (traffic)"),
            _("Cumulative users"),
            _("DDNS hostname"),
            _("Country name"),
        ]
        self.sort_dd = Gtk.DropDown.new(Gtk.StringList.new(sort_items), None)
        self.sort_dd.set_selected(0)
        self.sort_dd.connect("notify::selected-item", self._on_sort_changed)
        row_top.append(self.sort_dd)
        self.filters.append(row_top)

        row_bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.proto_seg = Segmented()
        proto_labels = {
            "all": _("All"),
            "sslvp": "SSL-VPN",
            "l2tp": "L2TP/IPsec",
            "sstp": "MS-SSTP",
            "openvpn": "OpenVPN",
        }
        self.proto_seg.set_options(
            ["all", "sslvp", "l2tp", "sstp", "openvpn"],
            labels=proto_labels)
        self.proto_seg.set_active("all")
        self.proto_seg.connect("changed", lambda _seg, _v: self._apply_filters())
        row_bottom.append(self.proto_seg)
        self.filters.append(row_bottom)

        return self.filters

    def _on_sort_changed(self, dropdown: Gtk.DropDown, _param) -> None:
        self._sort = _SORT_MODES[dropdown.get_selected()]
        self._apply_filters()

    def _on_search_changed(self, *args) -> None:       # noqa: ANN001
        # debounce: do not rebuild the whole card list per keystroke
        if self._search_id is not None:
            GLib.source_remove(self._search_id)
        self._search_id = GLib.timeout_add(250, self._search_timeout)

    def _search_timeout(self) -> bool:
        self._search_id = None
        self._apply_filters()
        return GLib.SOURCE_REMOVE

    def _build_list_stack(self) -> Gtk.Widget:
        self.list_stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self.list_stack.set_vhomogeneous(False)

        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.list_box = Gtk.ListBox(css_classes=["boxed-list"],
                                    selection_mode=Gtk.SelectionMode.SINGLE)
        self.list_box.connect("row-selected", self._on_row_selected)
        # NOTE: no "row-activated" connect — selecting a row only shows its
        # details in the sidebar; dialing stays an explicit action via the
        # hero button or "Connect with this server".
        scroller.set_child(self.list_box)
        self.list_stack.add_named(scroller, "list")

        self.page_empty = Adw.StatusPage(
            icon_name="ui-dialpad-symbolic", title=_("No servers yet"),
            description=_("Pull the daily VPN Gate relay list to find a "
                          "server. Mirror failures fall back to the CDN "
                          "snapshot automatically."))
        pull = Gtk.Button(label=_("Download the list"),
                          halign=Gtk.Align.CENTER,
                          css_classes=["suggested-action", "pill"])
        pull.connect("clicked", lambda *_: self.act_refresh())
        self.page_empty.set_child(pull)
        self.list_stack.add_named(self.page_empty, "empty")

        self.page_loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.list_stack.add_named(self.page_loading, "loading")

        self.page_noresult = Adw.StatusPage(
            icon_name="loupe-symbolic", title=_("No matching servers"),
            description=_("No relay matches your search or filters."))
        clear = Gtk.Button(label=_("Clear the filters"), halign=Gtk.Align.CENTER)
        clear.connect("clicked", lambda *_: self._clear_filters())
        self.page_noresult.set_child(clear)
        self.list_stack.add_named(self.page_noresult, "noresult")

        self.page_error = Adw.StatusPage(
            icon_name="network-offline-symbolic",
            title=_("Could not reach the relay directory"),
            description=_("Every mirror is unreachable. The CDN snapshot "
                          "may still work — try again or check your "
                          "connection."))
        retry = Gtk.Button(label=_("Retry"), halign=Gtk.Align.CENTER,
                           css_classes=["suggested-action", "pill"])
        retry.connect("clicked", lambda *_: self.act_refresh())
        self.page_error.set_child(retry)
        self.list_stack.add_named(self.page_error, "error")

        self.list_stack.set_visible_child_name("empty")
        return self.list_stack

    def _build_details(self) -> Gtk.Widget:
        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True,
            width_request=340)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_start=14, margin_end=14, margin_top=14,
                      margin_bottom=14)
        scroller.set_child(box)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.details_flag_box = Gtk.Box(valign=Gtk.Align.CENTER)
        self.details_country = Gtk.Label(xalign=0, wrap=True,
                                         css_classes=["title-4"],
                                         valign=Gtk.Align.CENTER)
        header.append(self.details_flag_box)
        header.append(self.details_country)
        box.append(header)

        self.details_host = Gtk.Label(xalign=0, wrap=True, selectable=True,
                                      css_classes=["dim"])
        box.append(self.details_host)

        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        self.details_grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        self.detail_values: Dict[str, Gtk.Label] = {}
        for index, (key, caption) in enumerate((
            ("speed", _("Line speed")), ("ping", _("Ping")),
            ("sessions", _("Sessions")), ("uptime", _("Uptime")),
            ("users", _("Total users")), ("traffic", _("Traffic")),
            ("operator", _("Operator")), ("isp", _("ISP host")),
            ("policy", _("Logging policy")),
        )):
            caption_label = Gtk.Label(xalign=0, label=caption,
                                      css_classes=["dim"])
            value = Gtk.Label(xalign=0, label="—", selectable=True, wrap=True,
                              max_width_chars=36)
            self.details_grid.attach(caption_label, 0, index, 1, 1)
            self.details_grid.attach(value, 1, index, 1, 1)
            self.detail_values[key] = value
        box.append(self.details_grid)

        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        box.append(Gtk.Label(xalign=0, label=_("Protocol"), css_classes=["dim"]))
        self.details_proto = Segmented()
        self.details_proto.connect("changed", self._on_details_proto_changed)
        box.append(self.details_proto)
        self.port_hint = Gtk.Label(xalign=0, wrap=True, css_classes=["dim"])
        box.append(self.port_hint)
        return scroller

    # -- log bottom sheet --------------------------------------------------------
    def _build_sheet(self) -> Gtk.Widget:
        initial_h = self.settings.get("log_height", 240)
        sheet = _SheetFallback(self.split, initial_height=initial_h)

        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                        margin_top=4, margin_start=14, margin_end=14,
                        margin_bottom=10)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        head.append(Gtk.Label(label=_("Connection log"), xalign=0,
                              css_classes=["title-4"]))
        self.sheet_pill = Gtk.Label(label="0", css_classes=["chip", "chip-slate"],
                                    tooltip_text=_("lines in the log"))
        head.append(self.sheet_pill)
        head.append(Gtk.Box(hexpand=True))
        self.follow = Gtk.CheckButton(label=_("Follow"), active=True)
        head.append(self.follow)
        copy_btn = Gtk.Button(icon_name="edit-copy-symbolic",
                              tooltip_text=_("Copy the whole log"))
        copy_btn.connect("clicked", self._copy_log)
        head.append(copy_btn)
        close_btn = Gtk.Button(icon_name="window-close-symbolic",
                               tooltip_text=_("Close the log (Esc)"))
        close_btn.connect("clicked", lambda *_: self._close_sheet())
        head.append(close_btn)
        panel.append(head)

        self.log_buffer = Gtk.TextBuffer()
        view = Gtk.TextView(buffer=self.log_buffer, editable=False,
                            wrap_mode=Gtk.WrapMode.WORD_CHAR,
                            css_classes=["vpn-log"])
        view.set_left_margin(10)
        view.set_right_margin(10)
        view.set_top_margin(8)
        view.set_bottom_margin(10)
        self.log_view = view
        scroller = Gtk.ScrolledWindow()
        scroller.props.child = view
        scroller.props.vexpand = True
        panel.append(scroller)
        sheet.props.sheet = panel

        for name, color in (("log-default", "#f0f3f6"), ("log-error", "#ff6d6d"),
                            ("log-warn", "#f4b661"), ("log-ok", "#7fe0a0"),
                            ("log-head", "#8aa0b4")):
            tag = self.log_buffer.create_tag(name)
            tag.props.foreground = color
        return sheet

    def _open_sheet(self) -> None:
        self.sheet.props.open = True
        self.sheet_btn.props.active = True
        self._log_follow()

    def _close_sheet(self) -> None:
        self.sheet.props.open = False
        self.sheet_btn.props.active = False

    def _on_sheet_btn(self, *args) -> None:            # noqa: ANN001
        if self.sheet_btn.get_active():
            self._open_sheet()
        else:
            self._close_sheet()

    def _log_follow(self) -> bool:
        if self.follow.get_active():
            end = self.log_buffer.get_end_iter()
            self.log_view.scroll_to_iter(end, 0.0, True, 1.0, 1.0)
        return False

    def copy_to_clipboard(self, text: str) -> None:
        copied = False
        try:
            cb = self.get_clipboard()
            if hasattr(cb, "set"):
                cb.set(text)
                copied = True
            elif hasattr(cb, "set_text"):
                cb.set_text(text)
                copied = True
        except Exception:
            pass
        if sys.platform == "win32":
            try:
                import subprocess
                subprocess.run(
                    ["clip.exe"],
                    input=text.encode("utf-16le"),
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                copied = True
            except Exception:
                pass

    def _copy_log(self, *args) -> None:                # noqa: ANN001
        start, end = self.log_buffer.get_bounds()
        text = self.log_buffer.get_text(start, end, False)
        self.copy_to_clipboard(text)
        self.toast(_("Log copied to the clipboard"))

    def toast(self, message: str, retry: bool = False) -> None:
        toast = Adw.Toast.new(GLib.markup_escape_text(str(message or "")))
        toast.props.timeout = 6
        if retry:
            toast.props.button_label = _("Retry")
            toast.props.priority = Adw.ToastPriority.HIGH
            toast.connect("button-clicked", lambda *_: self.act_refresh())
        self.toast_overlay.add_toast(toast)

    # ==================================================================
    # Adaptive layout / appearance
    # ==================================================================

    def _install_breakpoints(self) -> None:
        breakpoint = Adw.Breakpoint()
        breakpoint.set_condition(
            Adw.BreakpointCondition.parse("max-width: 860px"))
        breakpoint.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(breakpoint)

    def _apply_saved_appearance(self) -> None:
        self._set_color_scheme(self.settings.get("theme", "system"))

    def _set_color_scheme(self, scheme: str) -> None:
        manager = Adw.StyleManager.get_default()
        mapping = {"system": Adw.ColorScheme.DEFAULT,
                   "light": Adw.ColorScheme.FORCE_LIGHT,
                   "dark": Adw.ColorScheme.FORCE_DARK}
        try:
            manager.set_color_scheme(
                mapping.get(scheme, Adw.ColorScheme.DEFAULT))
        except Exception:                              # noqa: BLE001
            pass

    def _watch_network(self) -> None:
        monitor = Gio.NetworkMonitor.get_default()
        if monitor is None:
            return
        monitor.connect("network-changed", self._network_changed)
        self.offline_banner.props.revealed = not monitor.get_network_available()

    def _network_changed(self, _monitor, available: bool) -> None:
        self.offline_banner.props.revealed = not available

    # ==================================================================
    # Data plumbing
    # ==================================================================

    def _load_cached(self) -> bool:
        path = os.path.join(STATE_DIR, "servers.json")
        self.skeletonize()

        def task() -> None:
            servers: List[VpnServer] = []
            try:
                with open(path) as handle:
                    data = json.load(handle)
                servers = [_server_from_dict(item)
                           for item in data.get("servers", [])]
            except (OSError, ValueError):
                pass

            def deliver() -> bool:
                if servers:
                    self.set_servers(servers, source=_("the daily cache"))
                self.act_refresh()
                return False
            GLib.idle_add(deliver)
        threading.Thread(target=task, daemon=True).start()
        return False

    def skeletonize(self) -> None:
        child = self.page_loading.get_first_child()
        while child is not None:
            next_sibling = child.get_next_sibling()
            self.page_loading.remove(child)
            child = next_sibling
        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        container = Gtk.ListBox(css_classes=["boxed-list"],
                                selection_mode=Gtk.SelectionMode.NONE)
        for row in skeleton_rows(6):
            container.append(Gtk.ListBoxRow(child=row, selectable=False))
        scroller.props.child = container
        self.page_loading.append(scroller)
        self.list_stack.set_visible_child_name("loading")

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def act_focus_search(self) -> None:
        """Ctrl+F: move the keyboard focus to the search field."""
        self.search.grab_focus()

    def act_log_drawer(self) -> None:
        """Ctrl+L: open the log drawer (or close it when already open)."""
        if self.sheet.props.open:
            self._close_sheet()
        else:
            self._open_sheet()

    def act_escape(self) -> None:
        """Escape: close the log drawer when it is open."""
        if self.sheet.props.open:
            self._close_sheet()

    def act_refresh(self) -> None:
        if self._refreshing:
            return
        monitor = Gio.NetworkMonitor.get_default()
        if monitor is not None and not monitor.get_network_available():
            self.offline_banner.props.revealed = True
            return
        self._refreshing = True
        self.refresh_btn.set_sensitive(False)
        self.offline_banner.props.revealed = False
        if not self.servers:
            self.skeletonize()
        self.title_widget.props.subtitle = _("downloading the daily relay list …")

        mirrors = list(self.mirrors)

        def task() -> None:
            servers: List[VpnServer] = []
            error = ""
            source = ""
            try:
                servers, source = fetcher.fetch_servers(mirrors)
            except Exception as exc:               # noqa: BLE001
                error = str(exc)

            def deliver() -> bool:
                self._refresh_done(servers, error, source)
                return False
            GLib.idle_add(deliver)

        threading.Thread(target=task, daemon=True).start()

    def _refresh_done(self, servers: List[VpnServer], error: str,
                      source: str) -> None:
        self._refreshing = False
        self.refresh_btn.set_sensitive(True)
        if error or not servers:
            self.title_widget.props.subtitle = _("refresh failed")
            self.toast(_("Could not reach any mirror"), retry=True)
            self._append_log(_("Could not download the server list: {detail}")
                             .format(detail=(error or "empty result").strip()))
            if self.servers:
                self._apply_filters()
                self.list_stack.set_visible_child_name("list")
            else:
                self.list_stack.set_visible_child_name("error")
            return
        # Live-mirror fetches report their URL; repository/CDN snapshots do
        # not start with "http" and deserve the "a little older" banner.
        self.fallback_banner.props.revealed = not (source or "").startswith("http")
        self.set_servers(servers, source=source)
        self.title_widget.props.subtitle = _(
            "{count} public relay servers · updated {time}").format(
                count=len(servers), time=time.strftime("%H:%M"))

    def set_servers(self, servers: List[VpnServer], source: str = "") -> None:
        self.servers = list(servers)
        self._save_cache(servers)
        if source:
            self._append_log(_("Server list loaded from {source} — {count} "
                               "servers").format(source=source,
                                                 count=len(servers)))
        self.recommended = recommended_server(servers)
        self._reload_countries()
        self._build_quick_strip(servers)
        self._apply_filters()
        if self.selected is not None:
            self._show_details(self.selected)
        self._update_buttons()

    def _save_cache(self, servers: List[VpnServer]) -> None:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(os.path.join(STATE_DIR, "servers.json"), "w") as handle:
                json.dump({"servers": [s.to_dict() for s in servers]}, handle)
        except OSError:
            pass

    def _reload_countries(self) -> None:
        countries = sorted({s.country for s in self.servers if s.country})
        current = None
        selected = self.country_dd.get_selected_item()
        if selected is not None:
            current = selected.get_string()
        self.country_model = Gtk.StringList.new(
            [_("All countries")] + countries)
        self.country_dd.props.model = self.country_model
        if current is None or current == _("All countries"):
            self.country_dd.set_selected(0)
        elif current in countries:
            self.country_dd.set_selected(countries.index(current) + 1)
        else:
            self.country_dd.set_selected(0)

    # -- filtering -------------------------------------------------------------
    def _clear_filters(self) -> None:
        self.search.set_text("")
        self.country_dd.set_selected(0)
        self.proto_seg.set_active("all")

    def _apply_filters(self) -> None:
        needle = (self.search.get_text() or "").strip().lower()
        entry = self.country_dd.get_selected_item()
        country = None if entry is None or entry.get_string() == _("All countries") \
            else entry.get_string()
        chosen = self.proto_seg.get_active()
        wanted = None if not chosen or chosen == "all" else Protocol(chosen)

        visible: List[VpnServer] = []
        for server in self.servers:
            if needle:
                haystack = " ".join(
                    (server.country, server.country_code, server.hostname,
                     server.ip, server.operator)).lower()
                if needle not in haystack:
                    continue
            if country and server.country != country:
                continue
            if wanted and not server.supports(wanted):
                continue
            visible.append(server)
        self._rebuild_rows(visible)

    def _rebuild_rows(self, visible: List[VpnServer]) -> None:
        new_ids: Dict[int, VpnServer] = {}
        new_rows: Dict[int, Gtk.ListBoxRow] = {}
        for server in sort_servers(visible, self._sort):
            self._row_seq += 1
            idx = self._row_seq
            row = self._build_server_row(server)
            new_ids[idx] = server
            new_rows[idx] = row
            self.list_box.append(row)
        for idx, stale in list(self._rows.items()):
            if idx not in new_rows:
                try:
                    self.list_box.remove(stale)
                except Exception:                  # noqa: BLE001
                    pass
        self._row_for = new_ids
        self._rows = new_rows

        if visible:
            self.list_stack.set_visible_child_name("list")
        elif self.servers:
            self.list_stack.set_visible_child_name("noresult")
        else:
            self.list_stack.set_visible_child_name("empty")

        if self.selected is not None:
            alive = [s for s in visible if s is self.selected]
            if not alive:
                self.selected = None
                self.split.set_show_sidebar(False)

    def _build_server_row(self, server: VpnServer) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                       margin_top=8, margin_bottom=8,
                       margin_start=10, margin_end=10)
        line.append(flag_widget(server.country_code))

        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3,
                         hexpand=True, valign=Gtk.Align.CENTER)
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_box.append(Gtk.Label(
            label=server.hostname or server.ip or "?", xalign=0,
            ellipsize=Pango.EllipsizeMode.END, css_classes=["heading"]))
        if self.recommended is not None and server is self.recommended:
            title_box.append(Gtk.Label(
                label=_("★ Best now"), css_classes=["chip", "chip-teal"],
                tooltip_text=_("Highest scoring SSL-VPN relay right now")))
        center.append(title_box)

        center.append(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                css_classes=["dim"],
                                label=_("{country} · {sessions:,} sessions")
                                .format(country=server.country or "?",
                                        sessions=server.sessions)))
        chips = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        for proto in server.protocols:
            chips.append(protocol_chip(proto.value))
        center.append(chips)
        line.append(center)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                        valign=Gtk.Align.CENTER, halign=Gtk.Align.END)
        speed_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar = SpeedBar()
        bar.set_size_request(64, 9)
        bar.set_fraction(server.speed_mbps / 1000.0)
        speed_box.append(bar)
        speed_text = (f"{server.speed_mbps:.0f}"
                      if server.speed_mbps >= 100 else
                      f"{server.speed_mbps:.1f}") + " Mbps"
        speed_box.append(Gtk.Label(xalign=1, css_classes=["dim"],
                                   label=speed_text
                                   if server.speed_mbps else "—"))
        right.append(speed_box)
        right.append(ping_chip(server.ping_ms))
        line.append(right)
        row.set_child(line)
        return row

    def _on_row_selected(self, _box: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        self.selected = None
        for idx, candidate in self._rows.items():
            if candidate is row:
                self.selected = self._row_for[idx]
                break
        if self.selected is None:
            return
        self._show_details(self.selected)
        self.split.set_show_sidebar(True)

    # -- details ---------------------------------------------------------------
    def _show_details(self, server: VpnServer) -> None:
        while child := self.details_flag_box.get_first_child():
            self.details_flag_box.remove(child)
        self.details_flag_box.append(flag_widget(server.country_code, size=28))
        self.details_country.set_text(_("{country} relay").format(
            country=server.country or "?"))
        host = server.hostname or server.ip
        self.details_host.set_text("{}\nIP {}\nISP {}".format(
            host, server.ip or "—", server.isp_hostname or "—"))
        values = {
            "speed": server.speed_text,
            "ping": server.ping_text,
            "sessions": server.sessions_text,
            "uptime": server.uptime or "—",
            "users": f"{server.total_users:,}" if server.total_users else "—",
            "traffic": server.total_traffic or "—",
            "operator": server.operator or "—",
            "isp": server.isp_hostname or "—",
            "policy": server.log_policy or "—",
        }
        for key, value in values.items():
            self.detail_values[key].set_text(value)

        protocols = [p.value for p in server.protocols]
        remembered = self._protocol_choice.get(host, DEFAULT_PROTOCOL)
        fallback = remembered if remembered in protocols \
            else (protocols[0] if protocols else "")
        self.details_proto.set_options(
            protocols, labels={p: protocol_name(p) for p in protocols})
        self.details_proto.set_active(fallback)
        self._update_port_hint(server)

    def _update_port_hint(self, server: VpnServer) -> None:
        chosen = self.details_proto.get_active()
        try:
            protocol = Protocol(chosen)
        except ValueError:
            self.port_hint.set_text("")
            return
        hints = {
            Protocol.OPENVPN: _("OpenVPN uses the per-row .ovpn profile "
                                "published by the server; UDP preferred, "
                                "TCP fallback."),
            Protocol.L2TP: _("L2TP/IPsec uses the built-in client settings: "
                             "user 'vpn', password 'vpn', pre-shared key "
                             "'vpn'."),
            Protocol.SSTP: _("MS-SSTP tunnels over HTTPS (443); the relay "
                             "uses a self-signed certificate, which is not "
                             "verified."),
            Protocol.SSLVP: _("SSL-VPN speaks the SoftEther protocol "
                              "directly (hub VPNGATE, user vpn)."),
        }
        port = server.port_for(protocol)
        text = hints.get(protocol, "")
        if port:
            text += "\n" + _("Port") + f": {port}"
        self.port_hint.set_text(text)

    def _on_details_proto_changed(self, _seg: Segmented, value: str) -> None:
        if not self.selected or value in ("", "all"):
            return
        host = self.selected.hostname or self.selected.ip
        self._protocol_choice[host] = value
        self._update_port_hint(self.selected)

    # ==================================================================
    # Connecting
    # ==================================================================

    def _can_connect(self) -> bool:
        return self.state in ("idle", "error") and \
            (self.selected is not None or self.recommended is not None)

    def _on_connect_clicked(self, *args) -> None:      # noqa: ANN001
        if self.state in ("connecting", "authorizing"):
            if self._auth_dialog is not None:
                return
            self._abort_connect()
            return
        if self.state == "connected":
            self._disconnect()
            return
        if not self._can_connect():
            return
        server = self.selected or self.recommended
        hostname = server.hostname or server.ip
        try:
            protocol = Protocol(self._protocol_choice.get(hostname, DEFAULT_PROTOCOL))
        except ValueError:
            protocol = Protocol.SSTP if sys.platform == "win32" else Protocol.SSLVP
        if not server.supports(protocol):
            if server.protocols:
                protocol = server.protocols[0]
            else:
                self.toast(_("This relay announces no protocols"))
                return
        self._connect(server, protocol)

    def _on_quick_connect(self, _button, server: VpnServer) -> None:
        if not self._can_connect():
            return
        target_proto = Protocol.SSTP if sys.platform == "win32" else Protocol.SSLVP
        self._connect(server, target_proto)

    def act_connect_quick(self) -> None:
        """Connect via selection/recommended; disconnect when connected."""
        if self.state == "connected":
            self._disconnect()
            return
        self._on_connect_clicked()

    def _connect(self, server: VpnServer, protocol: Protocol) -> None:
        if not server.supports(protocol):
            self.toast(_("{host} does not speak this protocol").format(
                host=server.hostname or server.ip))
            return
        self._aborting = False
        self._hide_chips()
        timeout = int(self.settings.get("connect_timeout", DEFAULT_CONNECT_TIMEOUT))
        retries = int(self.settings.get("connect_retries", DEFAULT_CONNECT_RETRIES))
        if self.worker is not None and self.worker.poll() is None:
            self._send({"cmd": "connect", "protocol": protocol.value,
                        "server": server.to_dict(),
                        "timeout": timeout, "retries": retries})
            self._set_state("connecting",
                            _("Dialing {host} via {proto} …").format(
                                host=server.hostname or server.ip,
                                proto=protocol_name(protocol.value)))
            return
        self._pending = (server, protocol)
        self._start_authorization()

    def _abort_connect(self) -> None:
        """Stop pressed while dialing/authorizing: return to the idle view immediately."""
        if self.state not in ("connecting", "authorizing"):
            return
        self._aborting = True
        self._pending = None
        self._append_log(_("Stop pressed — aborting the connection attempt"))
        if self._auth_dialog is not None:
            try:
                self._auth_dialog.force_close()
            except Exception:                          # noqa: BLE001
                pass
        if self.worker is not None and self.worker.poll() is None:
            self.hero_sub.set_text(_("Stopping …"))
            self._send({"cmd": "abort"})
        else:
            self._aborting = False
            self._set_state("idle", _("Connection attempt stopped."))
        self._update_buttons()

    def _hide_chips(self) -> None:
        for chip in (self.chip_server, self.chip_tunnel, self.chip_public,
                     self.chip_iface, self.chip_uptime):
            chip.set_visible(False)

    # -- worker authorization ----------------------------------------------
    def _start_authorization(self) -> None:
        self._set_state("authorizing",
                        _("Requesting administrator privileges …"))
        threading.Thread(target=self._spawn_worker,
                         args=(self._sudo_password,),
                         daemon=True).start()

    def _spawn_worker(self, password: Optional[str]) -> None:
        method: str = ""
        proc = None
        try:
            proc, method = privilege.spawn(password)
        except privilege.BootstrapError as exc:
            reason = (str(exc) or "").strip() or "authentication failed"
            GLib.idle_add(self._bootstrap_failed, reason)
        except Exception as exc:                       # noqa: BLE001
            GLib.idle_add(self._bootstrap_failed, f"{type(exc).__name__}: {exc}")
        else:
            GLib.idle_add(self._worker_started, proc, method)

    def _bootstrap_failed(self, reason: str) -> bool:
        self._append_log(_("The helper could not be started: {detail}")
                         .format(detail=reason))
        if "polkit agent" in reason:
            self._append_log(_("Switching to the sudo fallback — enter your "
                               "password when prompted"))
            self._ask_password(self._password_received)
            return False
        if self._sudo_password:
            self._sudo_password = None
            self.auth_banner.props.revealed = True
            self.state = "idle"
            self.hero_ring.set_mode("error")
            self.hero_title.set_label(_("Connection failed"))
            self.hero_sub.set_text(_("Your password was rejected"))
            self._update_buttons()
            return False
        self.state = "idle"
        self.hero_ring.set_mode("error")
        self.hero_title.set_label(_("Connection failed"))
        self.hero_sub.set_text(reason)
        self.hero_sub.set_tooltip_text(reason)
        self._update_buttons()
        return False

    def _retry_authorize(self) -> None:
        self.auth_banner.props.revealed = False
        if self._can_connect():
            self.act_connect_quick()

    def _ask_password(self, done: Callable[[Optional[str]], None]) -> None:
        """Modal password request that does not block the main loop."""
        entry = Gtk.PasswordEntry(show_peek_icon=True, hexpand=True,
                                  placeholder_text=_("Administrator password"))
        dialog = Adw.AlertDialog(
            heading=_("Administrator password"),
            body=_("The VPN client needs root privileges to create the "
                   "tunnel. Please enter your password."),
            extra_child=entry)
        entry.connect("activate", lambda *_: dialog.response("continue"))
        self._auth_dialog = dialog
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("continue", _("Continue"))
        dialog.set_default_response("continue")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("continue",
                                       Adw.ResponseAppearance.SUGGESTED)

        def choose_done(dlg: Adw.AlertDialog, task: Gio.AsyncResult) -> None:
            try:
                dlg.choose_finish(task)
            except Exception:                      # noqa: BLE001
                pass
            self._auth_dialog = None
            value = entry.get_text() or ""
            GLib.idle_add(done, value or None)

        dialog.choose(self, None, choose_done)

    def _password_received(self, password: Optional[str]) -> None:
        self._auth_dialog = None
        if self._aborting:
            # Stop was pressed while the dialog was still up.
            self._aborting = False
            self._set_state("idle", _("Connection attempt stopped."))
            return
        if not password:
            self._append_log(_("No password entered; the helper stays off"))
            self.state = "idle"
            self.hero_ring.set_mode("idle")
            self.hero_title.set_label(_("Disconnected"))
            self.hero_sub.set_text(_("Authorization cancelled."))
            self._update_buttons()
            return
        self._sudo_password = password
        self._start_authorization()

    def _worker_started(self, proc, method: str) -> bool:   # noqa: ANN001
        self.worker = proc
        self.worker_method = method
        self._append_log(_("Helper ready (privileges via {method})")
                         .format(method=method))
        threading.Thread(target=self._read_worker, daemon=True).start()
        threading.Thread(target=self._read_worker_stderr, daemon=True).start()
        return False

    def _read_worker(self) -> None:
        assert self.worker is not None
        stream = self.worker.stdout
        if stream is None:
            return
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            GLib.idle_add(self._dispatch_event, event)
        GLib.idle_add(self._worker_gone)

    def _read_worker_stderr(self) -> None:
        assert self.worker is not None
        stream = self.worker.stderr
        if stream is None:
            return
        for line in stream:
            line = line.rstrip()
            if not line:
                continue
            if "incorrect password" in line or "authentication failure" in line:
                def deliver() -> bool:
                    self._sudo_password = None
                    self.auth_banner.props.revealed = True
                    self.toast(_("Your password was rejected"))
                    self.state = "idle"
                    self.hero_ring.set_mode("error")
                    self.hero_title.set_label(_("Connection failed"))
                    self.hero_sub.set_text(
                        _("The administrator password was rejected"))
                    self._update_buttons()
                    return False
                GLib.idle_add(deliver)
            else:
                GLib.idle_add(self._append_log, f"[helper] {line}")

    def _worker_gone(self) -> bool:
        self.worker = None
        self.worker_method = ""
        self._sudo_password = None
        self.connected_info = None
        self._session_started = 0.0
        self._aborting = False
        self._hide_chips()
        if self.state not in ("idle",):
            self.toast(_("The privileged helper stopped"))
        self.state = "idle"
        self.hero_ring.set_mode("idle")
        self.hero_title.set_label(_("Disconnected"))
        self.hero_sub.set_text(_("The helper stopped; back to idle."))
        self._update_buttons()
        return False

    def _send(self, command: dict) -> None:
        proc = self.worker
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.write(json.dumps(command) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            self._append_log(_("The helper process is gone"))

    # -- events --------------------------------------------------------------
    def _dispatch_event(self, event: dict) -> bool:
        kind = event.get("event")
        if kind == "log":
            self._append_log(event.get("text", ""))
        elif kind == "ready":
            uid = event.get("uid", 0)
            self._append_log(_("Helper ready as root (uid {uid})")
                             .format(uid=uid))
            if self._pending is not None:
                server, protocol = self._pending
                self._pending = None
                if self._aborting:
                    # Stop was pressed while the helper was spawning.
                    self._aborting = False
                    self._set_state("idle",
                                    _("Connection attempt stopped."))
                else:
                    self._connect(server, protocol)
        elif kind == "state":
            self._handle_state(event)
        elif kind == "connected":
            info = event.get("info", {})
            if self._aborting:
                # Race: the tunnel just came up while the user pressed
                # Stop — skip the connected visuals and bring it straight
                # down (the disconnect command is already queued; a second
                # one is harmless).
                self._append_log(_("Tunnel came up, but Stop was pressed — "
                                   "tearing it back down"))
                self._send({"cmd": "disconnect"})
            else:
                self.connected_info = info
                self._session_started = time.time()
                self._handle_connected(info)
        elif kind == "error":
            message = event.get("message") or _("unknown error")
            self._append_log(_("ERROR: {detail}").format(detail=message))
            if self._aborting:
                pass                         # stopping already; wait for it
            elif self.state == "connecting":
                self.hero_ring.set_mode("error")
                self.hero_title.set_label(_("Connection failed"))
                self.hero_sub.set_text(_("Connection failed. Check log for details."))
                self.state = "error"
                self._update_buttons()
                toast_msg = message.strip().splitlines()[0] if message else _("Connection failed")
                if len(toast_msg) > 60:
                    toast_msg = toast_msg[:57] + "…"
                self.toast(toast_msg)
        if kind == "bye":
            self._close_everything()
        return False

    def _handle_state(self, event: dict) -> None:
        state = event.get("state")
        if state == "connecting":
            if self._aborting:
                return                       # stop in flight; keep "Stopping …"
            self._hide_chips()
            self.hero_ring.set_mode("connecting")
            self.hero_title.set_label(_("Connecting…"))
            self.hero_sub.set_text(_("Connecting to relay …"))
            self.state = "connecting"
            self._update_buttons()
        elif state == "connected":
            if self._aborting:
                return                       # Stop wins; disconnect is queued
            info = self.connected_info or {}
            self.state = "connected"
            self.hero_ring.set_mode("connected")
            self.hero_title.set_label(_("Protected"))
            self.hero_sub.set_text(
                _("via {server} · {proto}").format(
                    server=info.get("server", "relay"),
                    proto=str(info.get("protocol", "")).upper()))
            self._update_buttons()
            self._handle_connected(info)
            self.toast(_("Connected via {server}").format(
                server=info.get("server", "relay")))
        elif state == "disconnected":
            stopped = self._aborting
            self._aborting = False
            if stopped:
                self.state = "idle"
                self.hero_ring.set_mode("idle")
                self.hero_title.set_label(_("Disconnected"))
                self.hero_sub.set_text(_("Connection attempt stopped."))
            elif self.state != "error":
                self.state = "idle"
                self.hero_ring.set_mode("idle")
                self.hero_title.set_label(_("Disconnected"))
                self.hero_sub.set_text(_("Disconnected."))
            self.connected_info = None
            self._session_started = 0.0
            self._has_logged_first_probe = False
            self._watchdog_failures = 0
            self._last_connectivity_log_time = 0.0
            self._hide_chips()
            self._update_buttons()

    def _handle_connected(self, info: dict) -> None:
        self._has_logged_first_probe = False
        self._watchdog_failures = 0
        self._last_connectivity_log_time = 0.0
        texts = (
            (self.chip_server, "🔒 " + str(info.get("server") or "-")),
            (self.chip_tunnel, _("Tunnel {ip}").format(ip=info.get("ip")
                                                       or "—")),
            (self.chip_public, _("Public {ip}").format(
                ip=info.get("external_ip") or "—")),
            (self.chip_iface, str(info.get("interface") or "—")),
            (self.chip_uptime, _("Up 00:00:00")),
        )
        for chip, text in texts:
            chip.set_label(text)
            chip.props.tooltip_text = text   # full text survives truncation
            chip.set_visible(True)

    # -- 1 Hz heartbeat ------------------------------------------------------
    def _tick(self) -> bool:
        if self.state in ("connecting", "authorizing"):
            # Animate the headline: "Connecting" → "Connecting." → ".." → "…"
            self._dot_phase = (self._dot_phase + 1) % 4
            pulse = ("", ".", "..", "…")[self._dot_phase]
            base = _("Connecting") if self.state == "connecting" \
                else _("Authorizing")
            self.hero_title.set_label(base + pulse)
        if self.state == "connected" and self.connected_info:
            elapsed = max(0, int(time.time() - self._session_started))
            hours, rest = divmod(elapsed, 3600)
            minutes, seconds = divmod(rest, 60)
            self.chip_uptime.set_label(_("Up {t}").format(
                t=f"{hours:02d}:{minutes:02d}:{seconds:02d}"))

            now = time.time()
            interval = float(self.settings.get("watchdog_interval", DEFAULT_WATCHDOG_INTERVAL))
            if not self._watchdog_in_flight and (now - self._watchdog_last_check) >= interval:
                self._watchdog_last_check = now
                self._watchdog_in_flight = True
                threading.Thread(target=self._run_watchdog_probe, daemon=True).start()
        return True

    def _run_watchdog_probe(self) -> None:
        targets = list(self.connectivity_targets)
        success = False
        reached_target = None
        rtt_ms = 0
        probe_timeout = float(self.settings.get("probe_timeout", DEFAULT_PROBE_TIMEOUT))
        for target in targets:
            t0 = time.time()
            if probe_target(target, timeout=probe_timeout):
                success = True
                reached_target = target
                rtt_ms = max(1, int((time.time() - t0) * 1000))
                break

        def on_probe_done() -> bool:
            self._watchdog_in_flight = False
            if self.state != "connected":
                self._watchdog_failures = 0
                return False

            now = time.time()
            has_logged = getattr(self, "_has_logged_first_probe", False)
            last_log = getattr(self, "_last_connectivity_log_time", 0.0)

            if success:
                self._watchdog_failures = 0
                if not has_logged:
                    self._has_logged_first_probe = True
                    self._last_connectivity_log_time = now
                    self._append_log(_("[connectivity] Internet verified: {target} reached in {ms} ms (tunnel carrying traffic)").format(target=reached_target, ms=rtt_ms))
                elif (now - last_log) >= 25.0:
                    self._last_connectivity_log_time = now
                    self._append_log(_("[connectivity] Probe OK: {target} responded in {ms} ms").format(target=reached_target, ms=rtt_ms))
            else:
                self._watchdog_failures += 1
                targets_str = ", ".join(targets)
                if not has_logged:
                    self._has_logged_first_probe = True
                    self._last_connectivity_log_time = now
                    self._append_log(_("[connectivity] Initial check warning: {targets} did not respond").format(targets=targets_str))
                else:
                    self._append_log(_("[connectivity] Check warning: {targets} unreachable (attempt {attempt}/2)").format(targets=targets_str, attempt=self._watchdog_failures))

                if self._watchdog_failures >= 2:
                    self._watchdog_failures = 0
                    self._append_log(_("[connectivity] ERROR: Connection lost — active connectivity check failed (targets unreachable)"))
                    self.toast(_("Connection lost: Internet or tunnel connection dropped."))
                    self.hero_ring.set_mode("error")
                    self.hero_title.set_label(_("Connection lost"))
                    self.hero_sub.set_text(_("Internet or tunnel connection dropped."))
                    self.state = "error"
                    self._update_buttons()
                    self._send({"cmd": "disconnect"})
            return False

        GLib.idle_add(on_probe_done)

    # -- button state ------------------------------------------------------
    def _set_state(self, state: str, message: Optional[str]) -> None:
        """Central state transition for the hero panel.

        Consumed by the dialing / authorization paths: moves the status
        ring, headline, subtitle and the action buttons in one go.  Other
        transitions (worker events) update the same widgets directly; this
        helper exists because those paths were written piecemeal.
        """
        if state not in ("connecting", "authorizing"):
            self._aborting = False
        self.state = state
        ring_mode = {"idle": "idle", "authorizing": "authorizing",
                     "connecting": "connecting", "connected": "connected",
                     "error": "error"}.get(state, "idle")
        self.hero_ring.set_mode(ring_mode)
        titles = {"idle": _("Disconnected"),
                  "authorizing": _("Authorizing…"),
                  "connecting": _("Connecting…"),
                  "connected": _("Protected"),
                  "error": _("Connection failed")}
        self.hero_title.set_label(titles.get(state, _("Disconnected")))
        if message:
            self.hero_sub.set_text(message)
        if state == "connected":
            self.title_widget.props.subtitle = _("tunnel up")
        elif state in ("connecting", "authorizing"):
            self.title_widget.props.subtitle = _("dialing")
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.connect_btn.remove_css_class("suggested-action")
        self.connect_btn.remove_css_class("destructive-action")
        if self.state == "connected":
            self.connect_btn.props.label = _("Disconnect")
            self.connect_btn.add_css_class("destructive-action")
            self.connect_btn.set_tooltip_text(_("Disconnect the tunnel"))
            self.connect_btn.set_sensitive(True)
        elif self.state in ("connecting", "authorizing"):
            # Stop: give up on the attempt and return to the previous view.
            self.connect_btn.props.label = _("Stop")
            self.connect_btn.add_css_class("destructive-action")
            self.connect_btn.set_tooltip_text(
                _("Stop the connection attempt and go back"))
            self.connect_btn.set_sensitive(True)
        else:                                       # idle / error
            self.connect_btn.props.label = _("Connect")
            self.connect_btn.add_css_class("suggested-action")
            self.connect_btn.set_tooltip_text(
                _("Connect now — your selection, or the best available "
                  "relay (Enter)"))
            self.connect_btn.set_sensitive(self._can_connect())
        self.header_spinner.set_visible(
            self.state in ("connecting", "authorizing"))
        if self.tray is not None:
            self.tray.set_state(self.state or "idle")
            self.tray.set_menu(
                connect_enabled=self._can_connect(),
                disconnect_enabled=self.state == "connected")

    # ==================================================================
    # Settings / about
    # ==================================================================

    def act_settings(self) -> None:
        dialog = Adw.PreferencesDialog()
        dialog.props.title = _("Settings")
        dialog.add(self._appearance_page())
        dialog.add(self._timeouts_page(dialog))
        dialog.add(self._connectivity_page(dialog))
        dialog.add(self._mirrors_page(dialog))
        dialog.add(self._support_page(dialog))
        dialog.props.search_enabled = False
        dialog.present(self)

    def _appearance_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=_("Appearance"),
                                   icon_name="applications-graphics-symbolic")
        group = Adw.PreferencesGroup(
            title=_("Color style"),
            description=_("Follow the desktop, or force one of the schemes"))
        options = [_("Follow system"), _("Light"), _("Dark")]
        setting_map = ["system", "light", "dark"]
        row = Adw.ComboRow(title=_("Theme"),
                           model=Gtk.StringList.new(options))
        current = self.settings.get("theme", "system")
        row.set_selected(setting_map.index(current)
                         if current in setting_map else 0)

        def changed(combo: Adw.ComboRow, _param) -> None:   # noqa: ANN001
            scheme = setting_map[combo.get_selected()]
            self.settings["theme"] = scheme
            save_settings(self.settings)
            self._set_color_scheme(scheme)
        row.connect("notify::selected", changed)
        group.add(row)
        page.add(group)
        return page

    def _timeouts_page(self, dialog: Optional[Adw.PreferencesDialog] = None) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=_("Timeouts & Retries"),
                                   icon_name="preferences-system-time-symbolic")

        group = Adw.PreferencesGroup(
            title=_("Connection Timeouts & Attempts"),
            description=_("Adjust waiting periods and retry behavior for tunnel dials and probes."))

        current_timeout = float(self.settings.get("connect_timeout", DEFAULT_CONNECT_TIMEOUT))
        row_timeout = _make_spin_row(
            _("Connection timeout (seconds)"),
            _("Time to wait for a tunnel dial before giving up or retrying"),
            10.0, 120.0, 5.0, current_timeout, digits=0)

        def on_timeout_changed(row, _param) -> None:
            self.settings["connect_timeout"] = int(row.get_value())
            save_settings(self.settings)

        row_timeout.connect("notify::value", on_timeout_changed)
        group.add(row_timeout)

        current_retries = float(self.settings.get("connect_retries", DEFAULT_CONNECT_RETRIES))
        row_retries = _make_spin_row(
            _("Dial attempts (retries)"),
            _("Maximum number of times to try establishing a connection to the selected relay"),
            1.0, 5.0, 1.0, current_retries, digits=0)

        def on_retries_changed(row, _param) -> None:
            self.settings["connect_retries"] = int(row.get_value())
            save_settings(self.settings)

        row_retries.connect("notify::value", on_retries_changed)
        group.add(row_retries)

        current_probe = float(self.settings.get("probe_timeout", DEFAULT_PROBE_TIMEOUT))
        row_probe = _make_spin_row(
            _("Connectivity probe timeout (seconds)"),
            _("Timeout for each individual network reachability check"),
            1.0, 15.0, 0.5, current_probe, digits=1)

        def on_probe_changed(row, _param) -> None:
            self.settings["probe_timeout"] = round(float(row.get_value()), 1)
            save_settings(self.settings)

        row_probe.connect("notify::value", on_probe_changed)
        group.add(row_probe)

        current_interval = float(self.settings.get("watchdog_interval", DEFAULT_WATCHDOG_INTERVAL))
        row_interval = _make_spin_row(
            _("Watchdog check interval (seconds)"),
            _("How often to verify Internet traffic while connected"),
            5.0, 60.0, 5.0, current_interval, digits=0)

        def on_interval_changed(row, _param) -> None:
            self.settings["watchdog_interval"] = int(row.get_value())
            save_settings(self.settings)

        row_interval.connect("notify::value", on_interval_changed)
        group.add(row_interval)

        page.add(group)

        reset_group = Adw.PreferencesGroup()
        reset_row = Adw.ActionRow(
            title=_("Reset timeouts"),
            subtitle=_("Restore default timeouts and retry counts"))
        reset_btn = Gtk.Button(label=_("Reset to defaults"),
                               css_classes=["flat", "destructive"],
                               valign=Gtk.Align.CENTER)

        def on_reset_clicked(_btn) -> None:
            self.settings["connect_timeout"] = DEFAULT_CONNECT_TIMEOUT
            self.settings["connect_retries"] = DEFAULT_CONNECT_RETRIES
            self.settings["probe_timeout"] = DEFAULT_PROBE_TIMEOUT
            self.settings["watchdog_interval"] = DEFAULT_WATCHDOG_INTERVAL
            save_settings(self.settings)
            row_timeout.set_value(DEFAULT_CONNECT_TIMEOUT)
            row_retries.set_value(DEFAULT_CONNECT_RETRIES)
            row_probe.set_value(DEFAULT_PROBE_TIMEOUT)
            row_interval.set_value(DEFAULT_WATCHDOG_INTERVAL)
            toast = Adw.Toast.new(_("Default timeouts and retries restored"))
            if dialog is not None and hasattr(dialog, "add_toast"):
                dialog.add_toast(toast)
            else:
                self.toast(_("Default timeouts and retries restored"))

        reset_btn.connect("clicked", on_reset_clicked)
        reset_row.add_suffix(reset_btn)
        reset_group.add(reset_row)
        page.add(reset_group)

        return page

    def _connectivity_page(self, dialog: Optional[Adw.PreferencesDialog] = None) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=_("Connectivity"),
                                   icon_name="network-wireless-symbolic")
        self._target_group = Adw.PreferencesGroup(
            title=_("Active connection watchdog endpoints"),
            description=_("Endpoints (IP addresses or hosts) probed periodically while connected to detect connection loss."))
        page.add(self._target_group)
        self._fill_target_rows(dialog)

        add_group = Adw.PreferencesGroup(title=_("Add test endpoint"))
        entry = Adw.EntryRow(title=_("IP address or host[:port]"), show_apply_button=True)
        error_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                            margin_start=12, margin_end=12, margin_top=4)
        error_label = Gtk.Label(label="", css_classes=["error-hint"],
                                xalign=0, wrap=True, visible=False)
        error_box.append(error_label)

        def on_entry_text_changed(row, _param) -> None:
            if error_label.get_visible():
                error_label.set_visible(False)

        entry.connect("notify::text", on_entry_text_changed)

        def add_target(_row) -> None:
            raw = (entry.get_text() or "").strip()
            if raw and len(raw) >= 3 and " " not in raw:
                error_label.set_visible(False)
                if raw not in self.connectivity_targets:
                    self.connectivity_targets.append(raw)
                    self._persist_targets()
                    self._fill_target_rows(dialog)
                    self._append_log(_("Test target added: {target}").format(target=raw))
                entry.set_text("")
                toast = Adw.Toast.new(_("Test endpoint added"))
                if dialog is not None and hasattr(dialog, "add_toast"):
                    dialog.add_toast(toast)
                else:
                    self.toast(_("Test endpoint added"))
            else:
                msg = _("Invalid host/IP. Enter e.g. 1.1.1.1, 8.8.8.8, or cloudflare.com:443")
                error_label.set_text(msg)
                error_label.set_visible(True)
                toast = Adw.Toast.new(msg)
                toast.props.timeout = 10
                if dialog is not None and hasattr(dialog, "add_toast"):
                    dialog.add_toast(toast)
                else:
                    self.toast(msg)

        entry.connect("apply", add_target)
        add_group.add(entry)
        add_group.add(error_box)
        page.add(add_group)

        reset_group = Adw.PreferencesGroup()
        reset_row = Adw.ActionRow(
            title=_("Test targets list"),
            subtitle=_("Restore default connectivity test IPs"))
        reset = Gtk.Button(label=_("Reset"),
                           css_classes=["flat", "destructive"])
        reset.connect("clicked", lambda *_: self._reset_targets(dialog))
        reset_row.add_suffix(reset)
        reset_group.add(reset_row)
        page.add(reset_group)
        return page

    def _persist_targets(self) -> None:
        self.settings["connectivity_targets"] = list(self.connectivity_targets)
        save_settings(self.settings)

    def _reset_targets(self, dialog: Optional[Adw.PreferencesDialog] = None) -> None:
        self.connectivity_targets = list(DEFAULT_CONNECTIVITY_TARGETS)
        self._persist_targets()
        self._fill_target_rows(dialog)
        toast = Adw.Toast.new(_("Default test endpoints restored"))
        if dialog is not None and hasattr(dialog, "add_toast"):
            dialog.add_toast(toast)
        else:
            self.toast(_("Default test endpoints restored"))

    def _fill_target_rows(self, dialog: Optional[Adw.PreferencesDialog] = None) -> None:
        group = getattr(self, "_target_group", None)
        if group is None:
            return
        old_rows = getattr(group, "_target_rows", [])
        for row in list(old_rows):
            try:
                if row.get_parent() is not None:
                    group.remove(row)
            except Exception:
                pass
        group._target_rows = []

        for target in list(self.connectivity_targets):
            row = Adw.ActionRow(title=target)

            test_btn = Gtk.Button(label=_("Test"),
                                  css_classes=["flat"],
                                  valign=Gtk.Align.CENTER)
            spinner = Gtk.Spinner(spinning=False,
                                  visible=False,
                                  valign=Gtk.Align.CENTER)
            test_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                               spacing=6,
                               valign=Gtk.Align.CENTER)
            test_box.append(spinner)
            test_box.append(test_btn)

            def make_test_handler(t: str, btn: Gtk.Button, sp: Gtk.Spinner):
                def handler(_btn):
                    btn.set_sensitive(False)
                    sp.set_visible(True)
                    sp.start()
                    self._append_log(_("[connectivity] Testing endpoint '{target}' ...").format(target=t))

                    def test_task():
                        ok, ms, details = probe_target_detailed(t, timeout=3.5)
                        def deliver():
                            sp.stop()
                            sp.set_visible(False)
                            btn.set_sensitive(True)
                            if ok:
                                msg = _("{target} is reachable ({ms} ms)").format(target=t, ms=ms)
                                self._append_log(_("[connectivity] ✓ Endpoint '{target}' is reachable in {ms} ms ({details})").format(
                                    target=t, ms=ms, details=details))
                            else:
                                msg = _("{target} is unreachable ({details})").format(target=t, details=details)
                                self._append_log(_("[connectivity] ✗ Endpoint '{target}' is unreachable ({details})").format(
                                    target=t, details=details))
                            toast = Adw.Toast.new(GLib.markup_escape_text(msg))
                            if dialog is not None and hasattr(dialog, "add_toast"):
                                dialog.add_toast(toast)
                            else:
                                self.toast(msg)
                            return False
                        GLib.idle_add(deliver)
                    threading.Thread(target=test_task, daemon=True).start()
                return handler
            test_btn.connect("clicked", make_test_handler(target, test_btn, spinner))
            row.add_suffix(test_box)

            rm_btn = Gtk.Button(icon_name="user-trash-symbolic",
                                css_classes=["flat", "circular"],
                                tooltip_text=_("Remove test endpoint"),
                                valign=Gtk.Align.CENTER)
            def make_rm_handler(t: str):
                def handler(_btn):
                    if t in self.connectivity_targets:
                        self.connectivity_targets.remove(t)
                        self._persist_targets()
                        self._fill_target_rows(dialog)
                        self._append_log(_("Test target removed: {target}").format(target=t))
                return handler
            rm_btn.connect("clicked", make_rm_handler(target))
            row.add_suffix(rm_btn)

            group.add(row)
            group._target_rows.append(row)

    def _mirrors_page(self, dialog: Optional[Adw.PreferencesDialog] = None) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=_("Mirror sites"),
                                   icon_name="globe-symbolic")
        self._mirror_group = Adw.PreferencesGroup(
            title=_("Daily relay list mirrors"),
            description=_("Ordered list of VPN Gate mirror sites. The "
                          "client tries them top to bottom and falls back "
                          "to the CDN snapshot."))
        page.add(self._mirror_group)
        self._fill_mirror_rows()

        add_group = Adw.PreferencesGroup(title=_("Add a mirror"))
        entry = Adw.EntryRow(title=_("New mirror URL"), show_apply_button=True)
        error_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                            margin_start=12, margin_end=12, margin_top=4)
        error_label = Gtk.Label(label="", css_classes=["error-hint"],
                                xalign=0, wrap=True, visible=False)
        error_box.append(error_label)

        def on_entry_text_changed(row, _param) -> None:
            if error_label.get_visible():
                error_label.set_visible(False)

        entry.connect("notify::text", on_entry_text_changed)

        def add_mirror(_row) -> None:              # noqa: ANN001
            url = (entry.get_text() or "").strip().rstrip("/")
            if (url.startswith("http://") or url.startswith("https://")) and len(url) > 8:
                error_label.set_visible(False)
                self.mirrors.append(url)
                self._persist_mirrors()
                self._fill_mirror_rows()
                self._append_log(_("Mirror added: {url}").format(url=url))
                entry.set_text("")
                toast = Adw.Toast.new(_("Mirror added successfully"))
                if dialog is not None and hasattr(dialog, "add_toast"):
                    dialog.add_toast(toast)
                else:
                    self.toast(_("Mirror added successfully"))
            else:
                msg = _("Invalid URL. Please enter a full address starting with http:// or https://")
                error_label.set_text(msg)
                error_label.set_visible(True)
                toast = Adw.Toast.new(msg)
                toast.props.timeout = 10
                if dialog is not None and hasattr(dialog, "add_toast"):
                    dialog.add_toast(toast)
                else:
                    self.toast(msg)

        entry.connect("apply", add_mirror)
        add_group.add(entry)
        add_group.add(error_box)
        page.add(add_group)

        tray_group = Adw.PreferencesGroup(title=_("Options"))
        tray_row = Adw.SwitchRow(
            title=_("Status icon in the system tray"),
            subtitle=_("Small helper process; goes away with the app"))
        tray_row.set_active(bool(self.settings.get("tray", True)))

        def tray_toggled(row: Adw.SwitchRow, _param) -> None:   # noqa: ANN001
            self.settings["tray"] = row.props.active
            save_settings(self.settings)
            if self.tray is None and row.props.active:
                self.setup_tray()
        tray_row.connect("notify::active", tray_toggled)
        tray_group.add(tray_row)
        page.add(tray_group)

        reset_group = Adw.PreferencesGroup()
        reset_row = Adw.ActionRow(
            title=_("Mirror list"),
            subtitle=_("Restore the built-in default mirrors"))
        reset = Gtk.Button(label=_("Reset"),
                           css_classes=["flat", "destructive"])
        reset.connect("clicked", lambda *_: self._reset_mirrors())
        reset_row.add_suffix(reset)
        reset_group.add(reset_row)
        page.add(reset_group)
        return page

    def _support_page(self, dialog: Optional[Adw.PreferencesDialog] = None) -> Adw.PreferencesPage:
        """The "Buy me a coffee" page: wallet addresses with copy buttons."""
        page = Adw.PreferencesPage(title=_("Buy me a coffee"),
                                   icon_name="star-symbolic")
        group = Adw.PreferencesGroup(
            title=_("Buy me a coffee"),
            description=_("If the client makes your day, a coffee is "
                          "always appreciated. Copy an address to send a "
                          "tip — every amount keeps the project brewing."))
        for label, address, note in (
                (_("Bitcoin"), DONATE_BITCOIN, "BTC"),
                (_("Ethereum"), DONATE_ETHEREUM, "ETH / ERC-20"),
                (_("BEP20 (BNB Smart Chain)"), DONATE_BEP20, "BEP20"),
        ):
            if address:
                row = Adw.ActionRow(title=label,
                                    subtitle=f"{address}  ·  {note}")
                copy_btn = Gtk.Button(icon_name="edit-copy-symbolic",
                                      tooltip_text=_("Copy the address"),
                                      css_classes=["flat", "circular"],
                                      valign=Gtk.Align.CENTER)
                copy_btn.connect("clicked",
                                 self._copy_donation_address, address, dialog)
                row.add_suffix(copy_btn)
            else:
                row = Adw.ActionRow(
                    title=label,
                    subtitle=_("Not set yet — add the address to "
                               "app/vpngate/config.py (DONATE_*)"))
            group.add(row)
        page.add(group)
        return page

    def _copy_donation_address(self, _button, address: str,
                               dialog: Optional[Adw.PreferencesDialog] = None) -> None:
        self.copy_to_clipboard(address)
        toast = Adw.Toast.new(_("Address copied to the clipboard"))
        if dialog is not None and hasattr(dialog, "add_toast"):
            dialog.add_toast(toast)
        else:
            self.toast(_("Address copied to the clipboard"))

    def _fill_mirror_rows(self) -> None:
        """Rebuild the mirror ActionRows for the active preferences dialog."""
        group = getattr(self, "_mirror_group", None)
        if group is None:
            return
        old_rows = getattr(group, "_mirror_rows", [])
        for row in list(old_rows):
            try:
                if row.get_parent() is not None:
                    group.remove(row)
            except Exception:                      # noqa: BLE001
                pass                               # non-child or already removed
        group._mirror_rows = []
        for index, url in enumerate(self.mirrors):
            row = Adw.ActionRow(title=url, title_lines=1)
            up = Gtk.Button(icon_name="go-up-symbolic",
                            tooltip_text=_("Move up"),
                            css_classes=["flat", "circular"],
                            sensitive=index > 0)
            up.connect("clicked", self._move_mirror, index, -1)
            down = Gtk.Button(icon_name="go-down-symbolic",
                              tooltip_text=_("Move down"),
                              css_classes=["flat", "circular"],
                              sensitive=index < len(self.mirrors) - 1)
            down.connect("clicked", self._move_mirror, index, +1)
            trash = Gtk.Button(icon_name="user-trash-symbolic",
                               tooltip_text=_("Remove"),
                               css_classes=["flat", "circular"])
            trash.connect("clicked", self._remove_mirror, index)
            check = Gtk.Button(icon_name="check-plain-symbolic",
                               tooltip_text=_("Validate this URL now"),
                               css_classes=["flat", "circular"])
            check.connect("clicked", self._validate_mirror, index)
            for widget in (check, up, down, trash):
                row.add_suffix(widget)
            group.add(row)
            group._mirror_rows.append(row)

    def _persist_mirrors(self) -> None:
        self.settings["mirrors"] = list(self.mirrors)
        save_settings(self.settings)

    def _move_mirror(self, _button, index: int, delta: int) -> None:   # noqa: ANN001
        target = index + delta
        if 0 <= target < len(self.mirrors):
            self.mirrors[index], self.mirrors[target] = \
                self.mirrors[target], self.mirrors[index]
            self._persist_mirrors()
            self._fill_mirror_rows()

    def _remove_mirror(self, _button, index: int) -> None:   # noqa: ANN001
        if 0 <= index < len(self.mirrors):
            removed = self.mirrors.pop(index)
            self._append_log(_("Mirror removed: {url}").format(url=removed))
            if not self.mirrors:
                self.mirrors = list(DEFAULT_MIRRORS)
            self._persist_mirrors()
            self._fill_mirror_rows()

    def _reset_mirrors(self) -> None:
        self.mirrors = list(DEFAULT_MIRRORS)
        self._persist_mirrors()
        self._fill_mirror_rows()
        self.toast(_("Mirror list reset"))

    def _validate_mirror(self, _button, index: int) -> None:   # noqa: ANN001
        if not (0 <= index < len(self.mirrors)):
            return
        url = self.mirrors[index]

        def task() -> None:
            ok = False
            try:
                body = fetcher.http_get(url + "/en/", timeout=25)
                ok = b"vg_table_row" in body or b"vg_hosts_table_id" in body
            except Exception:                      # noqa: BLE001
                ok = False

            def deliver() -> bool:
                if ok:
                    self.toast(_("{url} is reachable").format(url=url))
                else:
                    self.toast(_("{url} did not answer correctly").format(
                        url=url))
                return False
            GLib.idle_add(deliver)
        threading.Thread(target=task, daemon=True).start()

    def act_about(self) -> None:
        dialog = Adw.AboutDialog()
        dialog.props.application_name = APP_LOG
        dialog.props.application_icon = APP_ID
        dialog.props.version = VERSION
        dialog.props.developer_name = DEVELOPER_NAME
        dialog.props.website = GITHUB_URL
        dialog.props.comments = _(
            "A free-VPN client for the VPN Gate public relays (SSL-VPN / "
            "L2TP / MS-SSTP / OpenVPN) with a premium UX.")
        dialog.props.license_type = Gtk.License.MIT_X11
        dialog.props.developers = [DEVELOPER_NAME]
        dialog.present(self)

    # ==================================================================
    # Log / misc
    # ==================================================================

    def _append_log(self, text: str) -> bool:
        if not text:
            return False
        now = time.strftime("%H:%M:%S")
        for line in str(text).splitlines():
            line = line.strip()
            if not line:
                continue
            lower = line.lower()
            if "error" in lower or "failed" in lower or "cannot create" in lower:
                tag_name = "log-error"
            elif "warning" in lower or "timed out" in lower or "dropping" in lower or "unreachable" in lower:
                tag_name = "log-warn"
            elif "successfully" in lower or "connected in" in lower or "is active" in lower or "verified:" in lower or "ok:" in lower or "reachable" in lower:
                tag_name = "log-ok"
            elif line.startswith("[") and ("1/3" in line or "2/3" in line or "3/3" in line or "1/5" in line or "connectivity" in lower):
                tag_name = "log-head"
            else:
                tag_name = "log-default"
            end = self.log_buffer.get_end_iter()
            self.log_buffer.insert_with_tags_by_name(end, f"[{now}] {line}\n", tag_name)
        line_count = self.log_buffer.get_line_count()
        if line_count > 1400:
            trim = self.log_buffer.get_iter_at_line(line_count - 1100)
            self.log_buffer.delete(self.log_buffer.get_start_iter(), trim)
        self.sheet_pill.set_label(str(max(0, line_count - 1)))
        GLib.idle_add(self._log_follow)
        return False

    # ==================================================================
    # Tray / shutdown
    # ==================================================================

    def setup_tray(self) -> None:
        try:
            self.tray = Tray(ICONS_DIR, self._on_tray_action)
            self.tray.set_state(self.state or "idle")
            self.tray.set_menu(connect_enabled=self._can_connect(),
                               disconnect_enabled=False)
        except Exception as exc:                       # noqa: BLE001
            self.tray = None
            print(f"tray disabled: {exc}", file=sys.stderr)

    def _on_tray_action(self, action: str) -> None:
        if action == "show":
            GLib.idle_add(self.present)
        elif action == "connect":
            GLib.idle_add(self.act_connect_quick)
        elif action == "disconnect":
            GLib.idle_add(self._disconnect)
        elif action == "quit":
            GLib.idle_add(self._quit)

    def _disconnect(self) -> None:
        if self.state != "connected":
            return
        self._append_log(_("Disconnecting the tunnel …"))
        self._send({"cmd": "disconnect"})

    def _quit(self) -> None:
        self._send({"cmd": "quit"})
        if self.tray is not None:
            self.tray.stop()
        self.get_application().quit()

    def _close_everything(self) -> None:
        self.state = "idle"
        self._aborting = False
        self._hide_chips()
        self.connected_info = None
        self.hero_ring.set_mode("idle")
        self.hero_title.set_label(_("Disconnected"))
        self.hero_sub.set_text(_("Session closed."))
        self._update_buttons()

    def do_close_request(self, *_args) -> bool:
        self._quit()
        return True


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def main() -> None:
    app = VpnGateApp()
    # Optional smoke test: VG_UI_SELFTEST=1 quits after ~6 s and writes a
    # marker file, which lets an automation session verify the whole UI.
    # VG_UI_SELFTEST=2 additionally exercises the settings dialog and the
    # mirror row rebuild twice (regression test for the "tried to remove
    # non-child" Adwaita critical spam found in the field).
    if os.environ.get("VG_UI_SELFTEST"):
        stage_two = os.environ.get("VG_UI_SELFTEST") == "2"

        def stress() -> bool:
            win = app.win
            if win is not None and stage_two:
                win.act_settings()                 # builds _mirror_group
                win._fill_mirror_rows()            # removal path, twice
                win._fill_mirror_rows()
            return False

        def quit_marker() -> bool:
            try:
                marker_path = "/tmp/vg_selftest.txt" if os.path.isdir("/tmp") else os.path.join(tempfile.gettempdir(), "vg_selftest.txt")
                with open(marker_path, "w") as handle:
                    handle.write("SELFTEST-OK\n")
            except OSError:
                pass
            app.quit()
            return False

        if stage_two:
            GLib.timeout_add(2500, stress)
        GLib.timeout_add(6000, quit_marker)
    app.run(sys.argv)


if __name__ == "__main__":
    main()

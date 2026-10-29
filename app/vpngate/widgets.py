"""Custom widgets and the application stylesheet for the new UI.

Everything visual that is not a stock libadwaita widget lives here:

* :data:`CSS_BASE`/``CSS_GRADIENT`` - the application stylesheet, written
  against libadwaita's named palette so it follows the dark/light theme.
* :class:`StatusRing` - the animated connection emblem (idle / connecting /
  connected / error states) rendered with cairo on a ``Gtk.DrawingArea``.
* :class:`SpeedBar` - the miniature Mbps bar shown on every server card.
* :class:`Segmented` - the hand rolled radio-pill selector used for the
  protocol chooser (libadwaita 1.9's ``AdwToggleGroup`` cannot be filled
  at runtime, plain check buttons are fully theme aware).
* helpers: country flag emoji, protocol/ping chips, skeleton rows.

Cairo usage is kept simple (arcs, rounded rectangles) and the ticker
driven animation honours ``gtk-enable-animations`` so the desktop's
"reduce motion" preference is respected.
"""
from __future__ import annotations

import os
import sys
import time
from typing import List

import gi

gi.require_version("Gtk", "4.0")
import cairo  # noqa: E402
from gi.repository import GObject, Gtk  # noqa: E402
from gi.repository import Pango  # noqa: E402

# ---------------------------------------------------------------------------
# Stylesheet
# ---------------------------------------------------------------------------

CSS_BASE = """
/* ---- status chips ------------------------------------------------------ */
.chip {
    border-radius: 9999px;
    padding: 1px 10px;
    font-size: 80%;
    font-weight: 700;
    letter-spacing: 0.02em;
}
.chip-teal     { background-color: rgba(13,148,136,0.16);  color: #0f9d94; }
.chip-violet   { background-color: rgba(124,92,255,0.14);  color: #9077ff; }
.chip-amber    { background-color: rgba(217,119,6,0.15);   color: #d97706; }
.chip-blue     { background-color: rgba(28,113,216,0.15);  color: #4385e5; }
.chip-slate    { background-color: rgba(120,129,143,0.18); color: #9aa3b2; }

.chip-ping-ok   { background-color: rgba(34,197,94,0.14);  color: #26a45c; }
.chip-ping-mid  { background-color: rgba(217,119,6,0.15);  color: #d97706; }
.chip-ping-slow { background-color: rgba(239,68,68,0.14);  color: #ef4444; }

.flag-badge {
    background-color: alpha(@accent_color, 0.16);
    color: @accent_fg_color;
    border-radius: 6px;
    padding: 1px 6px;
    font-size: 80%;
    font-weight: 800;
    letter-spacing: 0.05em;
}
.error-hint {
    color: #ef4444;
    font-size: 88%;
    font-weight: 600;
}

/* ---- muted helper text -------------------------------------------------- */
.dim    { color: rgba(127,127,127,0.95); font-size: 90%; }
.dim-x  { color: alpha(@window_fg_color, 0.58); }

/* ---- segmented control --------------------------------------------------- */
.seg {
    min-height: 30px;
    padding: 0 14px;
    border-radius: 9999px;
    font-size: 92%;
    font-weight: 600;
    background-color: transparent;
}
.seg:hover:not(:checked) { background-color: alpha(@window_fg_color, 0.07); }
.seg:checked {
    background-color: alpha(@accent_color, 0.22);
    color: @accent_fg_color;
}

/* ---- rounded action buttons ---------------------------------------------- */
.vpn-connect {
    font-weight: 800;
    min-height: 42px;
    min-width: 132px;
    border-radius: 12px;
    font-size: 105%;
}
.vpn-disconnect {
    font-weight: 700;
    border-radius: 12px;
}

/* ---- hero / cards ---------------------------------------------------------- */
.hero {
    border-radius: 22px;
    padding: 16px 18px 14px 18px;
}
.hero-boxed { background-color: @card_bg_color; }
.hero-title { font-size: 148%; font-weight: 800; }
.hero-sub   { font-size: 94%; }
.quick-card {
    background-color: alpha(@accent_color, 0.09);
    border-radius: 14px;
    padding: 8px 12px;
}
.stat-chip {
    background-color: alpha(@window_fg_color, 0.06);
    border-radius: 10px;
    padding: 4px 10px;
    font-size: 90%;
}

/* ---- the terminal log -------------------------------------------------------- */
textview.vpn-log text {
    font-family: "DejaVu Sans Mono", "Ubuntu Mono", monospace;
    font-size: 88%;
    background-color: rgba(15,16,20,0.93);
    color: rgb(214,219,226);
}
.log-note {
    background-color: rgba(250,179,52,0.14);
    color: #d7a550;
    border-radius: 8px;
    padding: 3px 10px;
    font-size: 88%;
}

/* ---- skeleton shimmer ---------------------------------------------------------- */
@keyframes skel-pulse {
    from { opacity: 0.55; }
    50%  { opacity: 1.0; }
    to   { opacity: 0.55; }
}
.skel {
    background-color: alpha(@window_fg_color, 0.12);
    border-radius: 6px;
    animation: skel-pulse 1.4s ease-in-out infinite;
}
"""

# Optional hero gradient, loaded separately: a parser hiccup there must not
# take down the base styling.
CSS_GRADIENT = """
.hero-grad {
    background-image: linear-gradient(115deg,
        rgba(28,113,216,0.13) 0%,
        rgba(20,146,138,0.12) 55%,
        rgba(20,146,138,0.04) 100%);
}
"""


def apply_theme_css() -> None:
    import sys
    from gi.repository import Gdk
    display = Gdk.Display.get_default()
    for block in (CSS_BASE, CSS_GRADIENT):
        provider = Gtk.CssProvider()
        try:
            if hasattr(provider, "load_from_string"):
                provider.load_from_string(block)
            elif hasattr(provider, "load_from_data"):
                try:
                    provider.load_from_data(block.encode("utf-8"))
                except TypeError:
                    provider.load_from_data(block.encode("utf-8"), -1)
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        except Exception as exc:                      # noqa: BLE001
            print(f"stylesheet block ignored: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_flag_svg_path(code: str) -> str:
    code = (code or "").lower().strip()
    if not code:
        return ""
    candidates = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icons", "flags", f"{code}.svg"),
        os.path.join(os.path.dirname(sys.executable), "icons", "flags", f"{code}.svg"),
        os.path.join(getattr(sys, "_MEIPASS", ""), "icons", "flags", f"{code}.svg"),
        os.path.join(os.path.dirname(sys.executable), "_internal", "icons", "flags", f"{code}.svg"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return ""


def flag_emoji(code: str) -> str:
    """Turn a two letter country code into its flag emoji."""
    code = (code or "").upper().strip()
    if len(code) != 2 or not code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in code)


def flag_text(code: str) -> str:
    """Country emoji flag."""
    code = (code or "").upper().strip()
    if not code:
        return "🌐"
    return flag_emoji(code) or "🌐"


def flag_widget(code: str, size: int = 20) -> Gtk.Widget:
    """Return a country flag emoji label widget."""
    emoji = flag_text(code)
    lbl = Gtk.Label(label=emoji, css_classes=["flag"], valign=Gtk.Align.CENTER)
    if size >= 24:
        lbl.add_css_class("title-2")
    return lbl


def ping_level(ping_ms: int) -> str:
    """Class color of a latency value: ok / mid / slow."""
    if 0 < ping_ms <= 120:
        return "ok"
    if ping_ms <= 320:
        return "mid"
    return "slow"


_PING_CHIP = {"ok": "chip-ping-ok", "mid": "chip-ping-mid", "slow": "chip-ping-slow"}

_PROTOCOL_CHIP = {
    "sslvp": "chip-teal",
    "l2tp": "chip-violet",
    "sstp": "chip-amber",
    "openvpn": "chip-blue",
    "pptp": "chip-slate",
}

_PROTOCOL_NAME = {
    "sslvp": "SSL-VPN",
    "l2tp": "L2TP",
    "sstp": "MS-SSTP",
    "openvpn": "OpenVPN",
    "pptp": "PPTP",
}


def protocol_name(value: str) -> str:
    return _PROTOCOL_NAME.get(value, value.upper())


def protocol_chip(value: str) -> Gtk.Label:
    """A colored pill naming one offered protocol."""
    return Gtk.Label(label=protocol_name(value), tooltip_text=protocol_name(value),
                     css_classes=["chip", _PROTOCOL_CHIP.get(value, "chip-slate")])


def ping_chip(ping_ms: int) -> Gtk.Label:
    """The colored latency chip used on server cards and in details."""
    level = ping_level(ping_ms)
    text = f"{ping_ms} ms" if ping_ms > 0 else "n/a"
    return Gtk.Label(label=text, tooltip_text=f"Ping: {text}",
                     css_classes=["chip", _PING_CHIP[level]])


def stat_chip(text: str) -> Gtk.Label:
    """A soft rounded label used for tunnel details in the hero panel.

    Ellipsized with a bounded ``max-width-chars`` so a long server name can
    never push the hero panel (and the window) wider than it is.
    """
    chip = Gtk.Label(label=text, css_classes=["stat-chip", "dim-x"],
                     ellipsize=Pango.EllipsizeMode.END)
    chip.props.max_width_chars = 24
    return chip


def skeleton_rows(count: int = 5) -> List[Gtk.Widget]:
    """Grey shimmering placeholder rows for the server list."""
    rows: List[Gtk.Widget] = []
    for _ in range(count):
        row = Gtk.Box(margin_start=10, margin_end=10, margin_top=12,
                      margin_bottom=12, spacing=10)
        flag = Gtk.Box(css_classes=["skel"], halign=Gtk.Align.START)
        flag.set_size_request(40, 26)
        lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                        hexpand=True, valign=Gtk.Align.CENTER)
        top = Gtk.Box(css_classes=["skel"], halign=Gtk.Align.START)
        top.set_size_request(210, 16)
        bottom = Gtk.Box(css_classes=["skel"], halign=Gtk.Align.START,
                         margin_top=2)
        bottom.set_size_request(150, 12)
        lines.append(top)
        lines.append(bottom)
        bar = Gtk.Box(css_classes=["skel"], halign=Gtk.Align.END,
                      valign=Gtk.Align.CENTER)
        bar.set_size_request(70, 10)
        row.append(flag)
        row.append(lines)
        row.append(bar)
        rows.append(row)
    return rows


def animations_enabled() -> bool:
    """Honor the desktop's 'reduce motion' preference."""
    try:
        settings = Gtk.Settings.get_default()
        return bool(settings.get_property("gtk-enable-animations"))
    except (AttributeError, TypeError):
        return True


# ---------------------------------------------------------------------------
# Cairo geometry helpers
# ---------------------------------------------------------------------------

def round_rect(cr: cairo.Context, x: float, y: float, w: float, h: float,
               r: float) -> None:
    """Trace a rounded rectangle path on *cr*."""
    r = min(r, w / 2, h / 2)
    pi = 3.14159265358979
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -0.5 * pi, 0)
    cr.arc(x + w - r, y + h - r, r, 0, 0.5 * pi)
    cr.arc(x + r, y + h - r, r, 0.5 * pi, pi)
    cr.arc(x + r, y + r, r, pi, 1.5 * pi)
    cr.close_path()


def _hex(hexstr: str, alpha: float = 1.0) -> tuple:
    hexstr = (hexstr or "#000000").strip().lstrip("#")
    if len(hexstr) == 3:
        hexstr = "".join(ch * 2 for ch in hexstr)
    r = int(hexstr[0:2], 16) / 255.0
    g = int(hexstr[2:4], 16) / 255.0
    b = int(hexstr[4:6], 16) / 255.0
    return (r, g, b, alpha)


# ---------------------------------------------------------------------------
# StatusRing
# ---------------------------------------------------------------------------

class StatusRing(Gtk.DrawingArea):
    """The animated connection emblem at the heart of the hero panel.

    Modes: ``idle`` (dim padlock), ``authorizing`` (breathing ring),
    ``connecting`` (rotating sweep), ``connected`` (solid green ring, check
    mark, one rewarding pulse), ``error`` (red ring with dot).
    """

    MODE_COLOR = {
        "idle": "#7d8aa0",
        "authorizing": "#2aa79b",
        "connecting": "#2aa79b",
        "connected": "#22c55e",
        "error": "#ef4444",
    }

    def __init__(self) -> None:
        super().__init__()
        self.set_content_width(148)
        self.set_content_height(148)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.mode = "idle"
        self._t0 = time.monotonic()
        self._breathe_until = 0.0
        self._tick = None
        self.set_draw_func(self._draw, None)

    # -- lifecycle ----------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        if mode == self.mode:
            return
        self.mode = mode
        self._t0 = time.monotonic()
        self._breathe_until = 0.0
        if mode == "connected" and animations_enabled():
            self._breathe_until = self._t0 + 0.7      # one rewarding pulse
        if self._tick is None and (mode in ("connecting", "authorizing")
                                   or self._breathe_until):
            self._tick = self.add_tick_callback(self._on_tick)
        elif self._tick is not None:
            self._stop_tick()
        self.queue_draw()

    def _on_tick(self, _area, _clock, *args) -> bool:
        self.queue_draw()
        now = time.monotonic()
        if self.mode in ("connecting", "authorizing"):
            return True
        if now < self._breathe_until:
            return True
        self._stop_tick()
        return False

    def _stop_tick(self) -> None:
        if self._tick is not None:
            try:
                self.remove_tick_callback(self._tick)
            except (AttributeError, ValueError):
                pass
            self._tick = None

    # -- drawing -------------------------------------------------------------
    def _draw(self, area: Gtk.DrawingArea, cr: cairo.Context, w: int,
              h: int, _data) -> bool:
        if w <= 0 or h <= 0:
            return False
        s = min(w, h)
        cx, cy = w / 2, h / 2
        r = s / 2 - 13
        if r < 20:
            r = max(18, s / 2 - 8)
        color = self.MODE_COLOR.get(self.mode, self.MODE_COLOR["idle"])
        age = time.monotonic() - self._t0

        # background ring track
        cr.set_source_rgba(*_hex(color, 0.13))
        cr.set_line_width(7)
        cr.arc(cx, cy, r, 0, 2 * 3.14159265358979)
        cr.stroke()

        if self.mode == "connected":
            self._draw_connected(cr, cx, cy, r, color, age)
        elif self.mode == "connecting":
            self._draw_sweep(cr, cx, cy, r, color, age)
        elif self.mode == "authorizing":
            self._draw_breathe(cr, cx, cy, r, color, age)
        elif self.mode == "error":
            self._draw_error(cr, cx, cy, r, color, age)
        else:
            self._draw_idle(cr, cx, cy, r)
        return False

    def _draw_idle(self, cr, cx, cy, r) -> None:
        # a calm padlock silhouette inside the ring
        cr.set_source_rgba(*_hex("#8a97ac", 0.45))
        cr.set_line_width(2.6)
        sh = r * 0.42
        cr.arc(cx, cy + r * 0.06, sh, 3.14159265358979, 2 * 3.14159265358979)
        cr.stroke()
        bw = sh * 2.0
        bh = sh * 1.45
        by = cy + r * 0.06 - sh * 0.12
        round_rect(cr, cx - bw / 2, by, bw, bh, 5)
        cr.stroke()
        cr.set_source_rgba(*_hex("#8a97ac", 0.62))
        cr.arc(cx, by + bh * 0.16 + bh * 0.36, sh * 0.16, 0,
               2 * 3.14159265358979)
        cr.fill()

    def _draw_breathe(self, cr, cx, cy, r, color, age) -> None:
        pulse = 0.5 + 0.5 * (age % 2.0)
        radius = r * (0.985 + 0.02 * pulse)
        cr.set_source_rgba(*_hex(color, 0.22 + 0.5 * pulse))
        cr.set_line_width(5)
        cr.arc(cx, cy, radius, 0, 2 * 3.14159265358979)
        cr.stroke()

    def _draw_sweep(self, cr, cx, cy, r, color, age) -> None:
        two = 2 * 3.14159265358979
        rot = (age * 2.0) % two
        span = 1.35 + 0.25 * ((age % 2.0) / 2.0)
        cr.set_source_rgba(*_hex(color, 0.15))
        cr.set_line_width(15)
        cr.arc(cx, cy, r, rot, rot + span)
        cr.stroke()
        cr.set_source_rgba(*_hex(color, 0.92))
        cr.set_line_width(6)
        cr.arc(cx, cy, r, rot, rot + span)
        cr.stroke()

    def _draw_connected(self, cr, cx, cy, r, color, age) -> None:
        bump = max(0.0, 1.0 - age / 0.35) if self._breathe_until else 0.0
        scale = 1.0 + 0.055 * bump
        rr = r * scale
        cr.set_source_rgba(*_hex(color, 0.16 + 0.5 * bump))
        cr.set_line_width(13)
        cr.arc(cx, cy, rr + 3, 0, 2 * 3.14159265358979)
        cr.stroke()
        cr.set_source_rgba(*_hex(color, 1.0))
        cr.set_line_width(6)
        cr.arc(cx, cy, rr, 0, 2 * 3.14159265358979)
        cr.stroke()
        # check mark
        span = r * 0.36
        cr.set_line_width(max(2.5, span * 0.28))
        cr.set_line_cap(cairo.LineCap.ROUND)
        cr.set_line_join(cairo.LineJoin.ROUND)
        cr.move_to(cx - span * 0.9, cy + span * 0.05)
        cr.line_to(cx - span * 0.12, cy + span * 0.62)
        cr.line_to(cx + span * 0.95, cy - span * 0.55)
        cr.stroke()

    def _draw_error(self, cr, cx, cy, r, color, _age) -> None:
        cr.set_source_rgba(*_hex(color, 0.16))
        cr.set_line_width(13)
        cr.arc(cx, cy, r + 3, 0, 2 * 3.14159265358979)
        cr.stroke()
        cr.set_source_rgba(*_hex(color, 1.0))
        cr.set_line_width(6)
        cr.arc(cx, cy, r, 0, 2 * 3.14159265358979)
        cr.stroke()
        cr.arc(cx, cy, r * 0.16, 0, 2 * 3.14159265358979)
        cr.fill()


# ---------------------------------------------------------------------------
# SpeedBar
# ---------------------------------------------------------------------------

class SpeedBar(Gtk.DrawingArea):
    """Miniature Mbps bar (0..1) with a rounded track and teinted fill."""

    def __init__(self) -> None:
        super().__init__()
        self.set_content_width(74)
        self.set_content_height(10)
        self.set_valign(Gtk.Align.CENTER)
        self._fraction = 0.0
        self.set_draw_func(self._draw, None)

    def set_fraction(self, value: float) -> None:
        self._fraction = max(0.0, min(1.0, value))

    def _draw(self, area, cr: cairo.Context, w: int, h: int, _data) -> bool:
        if w <= 0 or h <= 0:
            return False
        radius = min(h / 2, 4)
        cr.set_source_rgba(*_hex("#98a2b3", 0.22))
        round_rect(cr, 0, 0, w, h, radius)
        cr.fill()
        fill_w = w * self._fraction
        if fill_w >= 2:
            lg = cairo.LinearGradient(0, 0, fill_w, 0)
            lg.add_color_stop_rgba(0, 0.149, 0.506, 0.847, 0.95)
            lg.add_color_stop_rgba(1, 0.302, 0.784, 0.746, 0.95)
            cr.set_source(lg)
            round_rect(cr, 0, 0, fill_w, h, radius)
            cr.fill()
        return False


# ---------------------------------------------------------------------------
# Segmented control
# ---------------------------------------------------------------------------

class Segmented(Gtk.Box):
    """A row of radio-style pill buttons, exactly one active at a time."""

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self) -> None:
        super().__init__(spacing=6,
                         css_classes=["type-seg"],
                         halign=Gtk.Align.START)
        self.first_button: Gtk.CheckButton | None = None
        self._buttons: dict[str, Gtk.CheckButton] = {}
        self._values: List[str] = []
        self._muting = False

    def set_options(self, values: List[str], labels: dict | None = None,
                    active: str | None = None) -> None:
        """Rebuild the pill row for *values* (order as given)."""
        labels = labels or {}
        child = self.get_first_child()
        while child is not None:
            next_sibling = child.get_next_sibling()
            self.remove(child)
            child = next_sibling
        self._buttons.clear()
        self._values = list(values)
        self.first_button = None
        self._muting = True
        for value in values:
            button = Gtk.CheckButton(label=labels.get(value, value),
                                     css_classes=["seg"])
            if self.first_button is None:
                self.first_button = button
            else:
                button.set_group(self.first_button)
            button.connect("toggled", self._on_toggled, value)
            self.append(button)
            self._buttons[value] = button
        self._muting = False
        self.set_active(active or (values[0] if values else ""))

    def set_active(self, value: str) -> None:
        """Select *value* without emitting ``changed``."""
        self._muting = True
        try:
            for name, button in self._buttons.items():
                button.set_active(name == value)
        finally:
            self._muting = False

    def get_active(self) -> str:
        for name, button in self._buttons.items():
            if button.get_active():
                return name
        return ""

    def set_sensitive(self, sensitive: bool = True) -> None:
        """Enable or disable every pill of the control."""
        for button in self._buttons.values():
            button.set_sensitive(sensitive)

    def _on_toggled(self, button: Gtk.CheckButton, value: str) -> None:
        if self._muting:
            return
        if button.get_active():
            self.emit("changed", value)

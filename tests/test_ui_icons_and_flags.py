"""Regression tests for Linux emoji flags and Linux/cross-platform ping icons."""
from __future__ import annotations

import configparser
import os
import sys
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest import mock

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP_DIR = os.path.join(ROOT_DIR, "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

if "gi" not in sys.modules:
    mock_gi = mock.MagicMock()
    sys.modules["gi"] = mock_gi
    mock_repo = mock.MagicMock()

    class DummyWindow:
        def __init__(self, *args, **kwargs):
            pass

    class DummyApp:
        def __init__(self, *args, **kwargs):
            pass

    mock_repo.Adw.ApplicationWindow = DummyWindow
    mock_repo.Adw.Application = DummyApp
    sys.modules["gi.repository"] = mock_repo
    sys.modules["cairo"] = mock.MagicMock()

from vpngate import ui, widgets  # noqa: E402
from vpngate.models import VpnServer  # noqa: E402


class TestFlagWidgetPlatformBehavior:
    def test_linux_uses_emoji_label_instead_of_svg_even_when_svg_exists(self):
        fake_label = mock.MagicMock(name="emoji_label")
        with mock.patch.object(widgets.sys, "platform", "linux"), \
             mock.patch.object(widgets.Gtk, "Label", return_value=fake_label) as mock_label, \
             mock.patch.object(widgets.Gtk.Image, "new_from_file") as mock_img_file:
            res = widgets.flag_widget("US", size=20)

        assert res is fake_label
        mock_img_file.assert_not_called()
        mock_label.assert_called_once_with(
            label="\U0001f1fa\U0001f1f8",
            css_classes=["flag"],
            valign=widgets.Gtk.Align.CENTER,
        )
        fake_label.add_css_class.assert_not_called()

    def test_linux_large_flag_adds_title2_class(self):
        fake_label = mock.MagicMock(name="large_emoji_label")
        with mock.patch.object(widgets.sys, "platform", "linux"), \
             mock.patch.object(widgets.Gtk, "Label", return_value=fake_label) as mock_label:
            res = widgets.flag_widget("JP", size=28)

        assert res is fake_label
        mock_label.assert_called_once_with(
            label="\U0001f1ef\U0001f1f5",
            css_classes=["flag"],
            valign=widgets.Gtk.Align.CENTER,
        )
        fake_label.add_css_class.assert_called_once_with("title-2")

    def test_linux_invalid_or_empty_code_uses_globe_emoji(self):
        fake_label = mock.MagicMock(name="globe_label")
        with mock.patch.object(widgets.sys, "platform", "linux"), \
             mock.patch.object(widgets.Gtk, "Label", return_value=fake_label) as mock_label:
            widgets.flag_widget("", size=20)

        mock_label.assert_called_once_with(
            label="\U0001f310",
            css_classes=["flag"],
            valign=widgets.Gtk.Align.CENTER,
        )

    def test_windows_keeps_svg_flag_image_when_svg_exists(self):
        expected_svg = os.path.join(APP_DIR, "icons", "flags", "us.svg")
        assert os.path.isfile(expected_svg)
        fake_img = mock.MagicMock(name="svg_flag")
        with mock.patch.object(widgets.sys, "platform", "win32"), \
             mock.patch.object(widgets.Gtk.Image, "new_from_file", return_value=fake_img) as mock_img_file, \
             mock.patch.object(widgets.Gtk, "Label") as mock_label:
            res = widgets.flag_widget("US", size=24)

        assert res is fake_img
        mock_label.assert_not_called()
        mock_img_file.assert_called_once()
        assert os.path.normcase(mock_img_file.call_args[0][0]) == os.path.normcase(expected_svg)
        fake_img.set_pixel_size.assert_called_once_with(24)
        fake_img.add_css_class.assert_called_once_with("flag-icon")

    def test_windows_falls_back_to_emoji_if_svg_missing(self):
        fake_label = mock.MagicMock(name="fallback_label")
        with mock.patch.object(widgets.sys, "platform", "win32"), \
             mock.patch.object(widgets.Gtk.Image, "new_from_file") as mock_img_file, \
             mock.patch.object(widgets.Gtk, "Label", return_value=fake_label) as mock_label:
            res = widgets.flag_widget("ZZ", size=20)

        assert res is fake_label
        mock_img_file.assert_not_called()
        mock_label.assert_called_once()


class TestBundledPingIconAssets:
    def test_hicolor_index_theme_includes_actions_and_apps(self):
        index_path = os.path.join(APP_DIR, "icons", "hicolor", "index.theme")
        assert os.path.isfile(index_path)
        cp = configparser.ConfigParser()
        cp.read(index_path, encoding="utf-8")
        dirs = cp.get("Icon Theme", "Directories").split(",")
        assert "scalable/apps" in dirs
        assert "scalable/actions" in dirs
        assert cp.has_section("scalable/actions")

    def test_symbolic_ping_and_stop_svgs_exist_and_are_valid(self):
        actions_dir = os.path.join(APP_DIR, "icons", "hicolor", "scalable", "actions")
        for filename in ("vpngate-ping-symbolic.svg", "vpngate-stop-symbolic.svg"):
            svg_path = os.path.join(actions_dir, filename)
            assert os.path.isfile(svg_path)
            tree = ET.parse(svg_path)
            root = tree.getroot()
            assert root.tag.endswith("svg")
            assert root.attrib.get("viewBox") == "0 0 16 16"
            assert root.attrib.get("width") == "16"
            assert root.attrib.get("height") == "16"
            fills = {el.attrib.get("fill") for el in root.iter() if "fill" in el.attrib}
            assert "#2e3436" in fills


class TestPingIconResolutionAndButtons:
    def test_register_icon_paths_adds_icons_and_actions_without_duplicates(self):
        fake_display = mock.MagicMock(name="display")
        fake_theme = mock.MagicMock(name="icon_theme")
        icons_root = os.path.join(APP_DIR, "icons")
        fake_theme.get_search_path.return_value = [icons_root]

        with mock.patch.object(ui.Gtk.IconTheme, "get_for_display", return_value=fake_theme):
            ui._register_icon_paths(fake_display)

        added = [call.args[0] for call in fake_theme.add_search_path.call_args_list]
        actions_path = os.path.join(APP_DIR, "icons", "hicolor", "scalable", "actions")
        apps_path = os.path.join(APP_DIR, "icons", "hicolor", "scalable", "apps")
        assert icons_root not in added  # Already present in existing search path
        assert actions_path in added
        assert apps_path in added
        assert len(added) == len(set(added))

    def test_resolve_icon_name_prefers_bundled_symbolic_icon(self):
        fake_theme = mock.MagicMock(name="theme")
        fake_theme.has_icon.side_effect = lambda name: name == "vpngate-ping-symbolic"
        with mock.patch.object(ui, "_register_icon_paths", return_value=fake_theme):
            assert ui._resolve_icon_name("vpngate-ping-symbolic", "network-transmit-receive-symbolic") == "vpngate-ping-symbolic"

        fake_theme.has_icon.side_effect = lambda name: name == "network-transmit-receive-symbolic"
        with mock.patch.object(ui, "_register_icon_paths", return_value=fake_theme):
            assert ui._resolve_icon_name("vpngate-ping-symbolic", "network-transmit-receive-symbolic") == "network-transmit-receive-symbolic"

    def test_make_icon_widget_falls_back_to_svg_file_if_theme_missing(self):
        fake_theme = mock.MagicMock(name="theme")
        fake_theme.has_icon.return_value = False
        fake_img = mock.MagicMock(name="file_img")
        with mock.patch.object(ui, "_register_icon_paths", return_value=fake_theme), \
             mock.patch.object(ui.Gtk.Image, "new_from_file", return_value=fake_img) as mock_file:
            res = ui._make_icon_widget(
                "vpngate-ping-symbolic",
                "vpngate-ping-symbolic.svg",
                "network-transmit-receive-symbolic",
                size=16,
            )

        assert res is fake_img
        mock_file.assert_called_once()
        assert mock_file.call_args[0][0].endswith("vpngate-ping-symbolic.svg")
        fake_img.set_pixel_size.assert_called_once_with(16)

    def test_build_filters_creates_ping_all_with_button_content_not_mixed_label_and_icon(self):
        win = ui.VpnGateWindow.__new__(ui.VpnGateWindow)
        win._protocol_filter = "all"
        win._country_filter = "all"
        win._sort = "score"
        win._search = ""
        win._ping_all_content = None
        win._ping_all_icon = None
        win._ping_all_label = None

        fake_theme = mock.MagicMock(name="theme")
        fake_theme.has_icon.side_effect = lambda n: n == "vpngate-ping-symbolic"
        content_obj = mock.MagicMock(name="button_content")
        ping_btn_obj = mock.MagicMock(name="ping_all_btn")

        def make_btn(*args, **kwargs):
            # Ensure label and icon_name are never passed together to Gtk.Button
            assert not ("label" in kwargs and "icon_name" in kwargs)
            if kwargs.get("child") is content_obj:
                return ping_btn_obj
            return mock.MagicMock()

        with mock.patch.object(ui, "_register_icon_paths", return_value=fake_theme), \
             mock.patch.object(ui.Adw, "ButtonContent", return_value=content_obj) as mock_bc, \
             mock.patch.object(ui.Gtk, "Button", side_effect=make_btn), \
             mock.patch.object(ui.Gtk, "Box", return_value=mock.MagicMock()), \
             mock.patch.object(ui.Gtk, "Label", return_value=mock.MagicMock()), \
             mock.patch.object(ui.Gtk, "DropDown", return_value=mock.MagicMock()), \
             mock.patch.object(ui.Gtk, "StringList", return_value=mock.MagicMock()), \
             mock.patch.object(ui.Gtk, "SearchEntry", return_value=mock.MagicMock()), \
             mock.patch.object(ui, "Segmented", return_value=mock.MagicMock()):
            win._build_filters()

        assert win.ping_all_btn is ping_btn_obj
        assert win._ping_all_content is content_obj
        mock_bc.assert_called_once_with(label="Ping All", icon_name="vpngate-ping-symbolic")

    def test_set_ping_all_state_updates_content_child_cleanly(self):
        win = ui.VpnGateWindow.__new__(ui.VpnGateWindow)
        win._ping_all_content = mock.MagicMock(name="content")
        win._ping_all_label = None
        win._ping_all_icon = None
        win.ping_all_btn = mock.MagicMock(name="btn")

        win._set_ping_all_state("Stop Ping", "process-stop-symbolic")
        win._ping_all_content.set_label.assert_called_with("Stop Ping")
        win._ping_all_content.set_icon_name.assert_called_with("process-stop-symbolic")
        win.ping_all_btn.set_label.assert_not_called()
        win.ping_all_btn.set_icon_name.assert_not_called()

        win._ping_all_content.reset_mock()
        win._set_ping_all_state("Stop (3/10)")
        win._ping_all_content.set_label.assert_called_once_with("Stop (3/10)")
        win._ping_all_content.set_icon_name.assert_not_called()

    def test_build_ping_row_button_uses_bundled_icon_or_svg_fallback(self):
        win = ui.VpnGateWindow.__new__(ui.VpnGateWindow)
        win._ping_single_server = mock.MagicMock()
        server = VpnServer(
            hostname="vpn1",
            ip="1.2.3.4",
            ping_ms=10,
            country="Japan",
            country_code="JP",
        )

        # Case 1: bundled symbolic icon found in Gtk.IconTheme
        fake_theme = mock.MagicMock(name="theme")
        fake_theme.has_icon.side_effect = lambda n: n == "vpngate-ping-symbolic"
        with mock.patch.object(ui, "_register_icon_paths", return_value=fake_theme), \
             mock.patch.object(ui.Gtk, "Button", return_value=mock.MagicMock()) as mock_btn:
            win._build_ping_row_button(server)
        assert mock_btn.call_args.kwargs["icon_name"] == "vpngate-ping-symbolic"
        assert mock_btn.call_args.kwargs["css_classes"] == ["flat", "circular"]

        # Case 2: icon theme reports all icon names missing -> falls back to bundled SVG file child
        fake_theme.has_icon.side_effect = None
        fake_theme.has_icon.return_value = False
        fake_img = mock.MagicMock(name="svg_fallback_img")
        with mock.patch.object(ui, "_register_icon_paths", return_value=fake_theme), \
             mock.patch.object(ui.Gtk.Image, "new_from_file", return_value=fake_img) as mock_file, \
             mock.patch.object(ui.Gtk, "Button", return_value=mock.MagicMock()) as mock_btn:
            win._build_ping_row_button(server)
        mock_file.assert_called_once()
        assert mock_file.call_args[0][0].endswith("vpngate-ping-symbolic.svg")
        assert mock_btn.call_args.kwargs["child"] is fake_img
        assert "image-button" in mock_btn.call_args.kwargs["css_classes"]

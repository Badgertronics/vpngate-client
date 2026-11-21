"""StatusNotifierItem / Win32 tray icon for the VPN Gate client.

Supports:
1. Windows: Native Win32 Shell_NotifyIcon via ctypes (zero external dependencies).
2. Linux / BSD: StatusNotifierItem + DBusMenu exported directly via Gio.DBusConnection
   on the session bus without needing GTK 3 or external packages.
3. Fallback: Dedicated helper process (tray_worker.py) if Gtk3 AppIndicator is desired.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from typing import Callable, Optional


# ============================================================================
# Windows Native Win32 Tray (ctypes)
# ============================================================================

class _Win32Tray:
    def __init__(self, icon_dir: str, on_action: Callable[[str], None]) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._user32 = ctypes.windll.user32
        self._shell32 = ctypes.windll.shell32
        self._kernel32 = ctypes.windll.kernel32

        self._icon_dir = os.path.abspath(icon_dir)
        self._on_action = on_action
        self._hwnd: Optional[int] = None
        self._nid = None
        self._current_hicon = None
        self._flags = {"connect_enabled": True, "disconnect_enabled": False}
        self._available = False
        self._ready_event = threading.Event()

        self._init_types()

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready_event.wait(timeout=3)

    def _init_types(self) -> None:
        ctypes = self._ctypes
        wintypes = self._wintypes

        self._LRESULT = wintypes.LPARAM
        self._WPARAM = wintypes.WPARAM
        self._LPARAM = wintypes.LPARAM

        self._user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, self._WPARAM, self._LPARAM]
        self._user32.DefWindowProcW.restype = self._LRESULT
        self._WNDPROC = ctypes.WINFUNCTYPE(self._LRESULT, wintypes.HWND, wintypes.UINT, self._WPARAM, self._LPARAM)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("style", wintypes.UINT),
                ("lpfnWndProc", self._WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HCURSOR),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
                ("hIconSm", wintypes.HICON),
            ]
        self._WNDCLASSEXW = WNDCLASSEXW

        class NOTIFYICONDATAW(ctypes.Structure):
            class VERSION_OR_TIMEOUT(ctypes.Union):
                _fields_ = [
                    ("uTimeout", wintypes.UINT),
                    ("uVersion", wintypes.UINT),
                ]

            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", wintypes.ULONG),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", wintypes.BYTE * 8),
                ]

            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("version_or_timeout", VERSION_OR_TIMEOUT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", GUID),
                ("hBalloonIcon", wintypes.HICON),
            ]
            _anonymous_ = ["version_or_timeout"]
        self._NOTIFYICONDATAW = NOTIFYICONDATAW

        self._user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ]
        self._user32.CreateWindowExW.restype = wintypes.HWND
        self._user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
        self._user32.RegisterClassExW.restype = wintypes.ATOM
        self._shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        self._shell32.Shell_NotifyIconW.restype = wintypes.BOOL

    def is_available(self) -> bool:
        return self._available

    def _get_app_icon_handle(self) -> Optional[int]:
        hinst = self._kernel32.GetModuleHandleW(None)
        module_dir = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(self._icon_dir, "vpngate.ico"),
            os.path.join(os.path.dirname(self._icon_dir), "vpngate.ico"),
            os.path.join(os.path.dirname(os.path.dirname(self._icon_dir)), "vpngate.ico"),
            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(self._icon_dir))), "vpngate.ico"),
            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(self._icon_dir)))), "vpngate.ico"),
            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(self._icon_dir)))), "app", "icons", "vpngate.ico"),
            os.path.join(module_dir, "icons", "vpngate.ico"),
            os.path.join(os.path.dirname(module_dir), "icons", "vpngate.ico"),
            os.path.join(os.path.dirname(module_dir), "app", "icons", "vpngate.ico"),
        ]
        if getattr(sys, "frozen", False):
            base_dir = os.path.dirname(sys.executable)
            candidates.extend([
                os.path.join(base_dir, "icons", "vpngate.ico"),
                os.path.join(base_dir, "_internal", "icons", "vpngate.ico"),
            ])
            if hasattr(sys, "_MEIPASS"):
                candidates.extend([
                    os.path.join(sys._MEIPASS, "icons", "vpngate.ico"),
                    os.path.join(sys._MEIPASS, "app", "icons", "vpngate.ico"),
                ])

        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x0010
        LR_DEFAULTSIZE = 0x0040
        for p in candidates:
            if os.path.isfile(p):
                try:
                    h = self._user32.LoadImageW(None, os.path.abspath(p), IMAGE_ICON, 0, 0,
                                                LR_LOADFROMFILE | LR_DEFAULTSIZE)
                    if h:
                        return h
                except Exception:
                    pass

        # Try extracting application icon embedded in executable
        try:
            exe = sys.executable
            if exe and os.path.isfile(exe):
                h = self._shell32.ExtractIconW(hinst, exe, 0)
                if h and int(h) > 1:
                    return h
        except Exception:
            pass

        # Fallback to standard Windows application icon
        try:
            IDI_APPLICATION = 32512
            h = self._user32.LoadIconW(0, IDI_APPLICATION)
            if h:
                return h
        except Exception:
            pass

        return None

    def _get_icon_handle(self, _name: str = "") -> Optional[int]:
        return self._get_app_icon_handle()

    def _wndproc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        WM_USER = 0x0400
        WM_TRAY = WM_USER + 20
        WM_CLOSE = 0x0010
        WM_DESTROY = 0x0002
        WM_LBUTTONUP = 0x0202
        WM_LBUTTONDBLCLK = 0x0203
        WM_RBUTTONUP = 0x0205
        WM_CONTEXTMENU = 0x007B

        if msg == WM_TRAY:
            if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self._on_action("show")
                return 0
            if lparam in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._show_menu()
                return 0
        elif msg == WM_CLOSE:
            self._user32.DestroyWindow(hwnd)
            return 0
        elif msg == WM_DESTROY:
            self._user32.PostQuitMessage(0)
            return 0
        return self._user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _show_menu(self) -> None:
        ctypes = self._ctypes
        wintypes = self._wintypes

        MF_STRING = 0x0000
        MF_SEPARATOR = 0x0800
        MF_GRAYED = 0x0001
        TPM_RETURNCMD = 0x0100
        TPM_RIGHTBUTTON = 0x0002

        hmenu = self._user32.CreatePopupMenu()
        self._user32.AppendMenuW(hmenu, MF_STRING, 101, "Show VPN Gate")
        self._user32.SetMenuDefaultItem(hmenu, 101, 0)

        c_flag = MF_STRING if self._flags.get("connect_enabled") else (MF_STRING | MF_GRAYED)
        self._user32.AppendMenuW(hmenu, c_flag, 102, "Connect (recommended)")

        d_flag = MF_STRING if self._flags.get("disconnect_enabled") else (MF_STRING | MF_GRAYED)
        self._user32.AppendMenuW(hmenu, d_flag, 103, "Disconnect")

        self._user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
        self._user32.AppendMenuW(hmenu, MF_STRING, 104, "Quit")

        pt = wintypes.POINT()
        self._user32.GetCursorPos(ctypes.byref(pt))

        self._user32.SetForegroundWindow(self._hwnd)
        cmd = self._user32.TrackPopupMenuEx(hmenu, TPM_RETURNCMD | TPM_RIGHTBUTTON, pt.x, pt.y, self._hwnd, None)
        self._user32.PostMessageW(self._hwnd, 0, 0, 0)
        self._user32.DestroyMenu(hmenu)

        if cmd == 101:
            self._on_action("show")
        elif cmd == 102:
            self._on_action("connect")
        elif cmd == 103:
            self._on_action("disconnect")
        elif cmd == 104:
            self._on_action("quit")

    def _run(self) -> None:
        ctypes = self._ctypes
        wintypes = self._wintypes

        # Attach to Default interactive desktop if possible
        try:
            hwinsta = self._user32.OpenWindowStationW("WinSta0", False, 0x037F)
            if hwinsta:
                self._user32.SetProcessWindowStation(hwinsta)
            h_default = self._user32.OpenDesktopW("Default", 0, False, 0x01FF)
            if h_default:
                self._user32.SetThreadDesktop(h_default)
        except Exception:
            pass

        self._wndproc_ptr = self._WNDPROC(self._wndproc)
        hinst = self._kernel32.GetModuleHandleW(None)
        cls_name = f"VpnGateTrayCls_{os.getpid()}_{id(self)}"

        wcex = self._WNDCLASSEXW()
        wcex.cbSize = ctypes.sizeof(self._WNDCLASSEXW)
        wcex.lpfnWndProc = self._wndproc_ptr
        wcex.hInstance = hinst
        wcex.lpszClassName = cls_name

        atom = self._user32.RegisterClassExW(ctypes.byref(wcex))
        if not atom:
            self._ready_event.set()
            return

        hwnd = self._user32.CreateWindowExW(0, cls_name, "VpnGateTray", 0, 0, 0, 0, 0, None, None, hinst, None)
        if not hwnd:
            self._ready_event.set()
            return

        self._hwnd = hwnd
        self._current_hicon = self._get_app_icon_handle()

        WM_USER = 0x0400
        NIF_MESSAGE = 0x00000001
        NIF_ICON = 0x00000002
        NIF_TIP = 0x00000004
        NIM_ADD = 0x00000000
        NIM_DELETE = 0x00000002

        nid = self._NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(self._NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_USER + 20
        nid.hIcon = self._current_hicon or 0
        nid.szTip = "VPN Gate Client - disconnected"
        self._nid = nid

        ret = self._shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        self._available = bool(ret)
        self._ready_event.set()

        msg = wintypes.MSG()
        while self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            self._user32.TranslateMessage(ctypes.byref(msg))
            self._user32.DispatchMessageW(ctypes.byref(msg))

        if self._nid and self._available:
            self._shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            self._available = False
        if self._current_hicon:
            self._user32.DestroyIcon(self._current_hicon)
            self._current_hicon = None

    def set_state(self, status: str) -> None:
        if not self._available or not self._hwnd or not self._nid:
            return
        mapping = {
            "idle": "VPN Gate Client - disconnected",
            "authorizing": "VPN Gate Client - requesting privileges ...",
            "connecting": "VPN Gate Client - connecting ...",
            "connected": "VPN Gate Client - connected",
            "error": "VPN Gate Client - error",
        }
        text = mapping.get(status, mapping["idle"])
        self._nid.szTip = text[:127]
        if not self._nid.hIcon and self._current_hicon:
            self._nid.hIcon = self._current_hicon
        NIM_MODIFY = 0x00000001
        self._shell32.Shell_NotifyIconW(NIM_MODIFY, self._ctypes.byref(self._nid))

    def notify(self, title: str, message: str, level: str = "info") -> bool:
        if not self._available or not self._hwnd or not self._nid:
            return False
        try:
            try:
                hwinsta = self._user32.OpenWindowStationW("WinSta0", False, 0x037F)
                if hwinsta:
                    self._user32.SetProcessWindowStation(hwinsta)
                hdesk = self._user32.OpenDesktopW("Default", 0, False, 0x01FF)
                if hdesk:
                    self._user32.SetThreadDesktop(hdesk)
            except Exception:
                pass

            NIF_INFO = 0x00000010
            NIIF_INFO = 0x00000001
            NIIF_WARNING = 0x00000002
            flag = NIIF_WARNING if level in ("warning", "error", "alarm") else NIIF_INFO

            self._nid.uFlags = 1 | 2 | 4 | NIF_INFO
            self._nid.szInfoTitle = title[:63]
            self._nid.szInfo = message[:255]
            self._nid.dwInfoFlags = flag
            NIM_MODIFY = 0x00000001
            ret = self._shell32.Shell_NotifyIconW(NIM_MODIFY, self._ctypes.byref(self._nid))
            return bool(ret)
        except Exception:
            return False

    def set_menu(self, **flags: bool) -> None:
        self._flags.update(flags)

    def stop(self) -> None:
        if self._hwnd:
            WM_CLOSE = 0x0010
            self._user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        self._thread.join(timeout=2)


def win32_standalone_notify(title: str, message: str, level: str = "info") -> bool:
    """Show a Windows desktop toast/balloon notification without requiring an active tray object."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        import time

        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32

        try:
            hwinsta = user32.OpenWindowStationW("WinSta0", False, 0x037F)
            if hwinsta:
                user32.SetProcessWindowStation(hwinsta)
            hdesk = user32.OpenDesktopW("Default", 0, False, 0x01FF)
            if hdesk:
                user32.SetThreadDesktop(hdesk)
        except Exception:
            pass

        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = wintypes.LPARAM
        WNDPROC = ctypes.WINFUNCTYPE(wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        def wndproc(hwnd, msg, wparam, lparam):
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        wndproc_ptr = WNDPROC(wndproc)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HCURSOR),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
                ("hIconSm", wintypes.HICON),
            ]

        cls_name = f"VpnGateNotifyCls_{os.getpid()}_{time.time_ns()}"
        hinst = kernel32.GetModuleHandleW(None)
        wcex = WNDCLASSEXW()
        wcex.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wcex.lpfnWndProc = wndproc_ptr
        wcex.hInstance = hinst
        wcex.lpszClassName = cls_name
        user32.RegisterClassExW(ctypes.byref(wcex))

        hwnd = user32.CreateWindowExW(0, cls_name, "VpnGateNotify", 0, 0, 0, 0, 0, None, None, hinst, None)
        if not hwnd:
            return False

        class VERSION_OR_TIMEOUT(ctypes.Union):
            _fields_ = [("uTimeout", wintypes.UINT), ("uVersion", wintypes.UINT)]
        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.ULONG), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", wintypes.BYTE * 8)]
        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("version_or_timeout", VERSION_OR_TIMEOUT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", GUID),
                ("hBalloonIcon", wintypes.HICON),
            ]
            _anonymous_ = ["version_or_timeout"]

        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL

        icon_candidates = [
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icons", "vpngate.ico"),
            os.path.join(os.path.dirname(sys.executable), "icons", "vpngate.ico") if getattr(sys, "frozen", False) else "",
            os.path.join(os.path.dirname(sys.executable), "_internal", "icons", "vpngate.ico") if getattr(sys, "frozen", False) else "",
        ]
        hicon = None
        for p in icon_candidates:
            if p and os.path.isfile(p):
                hicon = user32.LoadImageW(None, os.path.abspath(p), 1, 0, 0, 0x0010 | 0x0040)
                if hicon:
                    break
        if not hicon and getattr(sys, "frozen", False) and sys.executable:
            hicon = shell32.ExtractIconW(hinst, sys.executable, 0)
        if not hicon or int(hicon) <= 1:
            hicon = user32.LoadIconW(0, 32512)

        flag = 0x00000002 if level in ("warning", "error", "alarm") else 0x00000001
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = 1 | 2 | 4 | 0x00000010
        nid.uCallbackMessage = 0x0400 + 20
        nid.hIcon = hicon
        nid.szTip = "VPN Gate Client"
        nid.szInfoTitle = title[:63]
        nid.szInfo = message[:255]
        nid.dwInfoFlags = flag

        ret = shell32.Shell_NotifyIconW(0, ctypes.byref(nid))

        def cleanup():
            time.sleep(4)
            try:
                shell32.Shell_NotifyIconW(2, ctypes.byref(nid))
                user32.DestroyWindow(hwnd)
                if hicon and int(hicon) > 1:
                    user32.DestroyIcon(hicon)
            except Exception:
                pass

        threading.Thread(target=cleanup, daemon=True).start()
        return bool(ret)
    except Exception:
        return False


# ============================================================================
# Linux D-Bus StatusNotifierItem + DBusMenu (Gio)
# ============================================================================

SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Activate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg name="delta" type="i" direction="in"/>
      <arg name="orientation" type="s" direction="in"/>
    </method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus">
      <arg name="status" type="s"/>
    </signal>
    <signal name="NewMenu"/>
  </interface>
</node>
"""

DBUSMENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/>
      <arg name="name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <signal name="LayoutUpdated">
      <arg name="revision" type="u"/>
      <arg name="parent" type="i"/>
    </signal>
  </interface>
</node>
"""


class _DBusTray:
    def __init__(self, icon_dir: str, on_action: Callable[[str], None]) -> None:
        from gi.repository import Gio, GLib

        self._Gio = Gio
        self._GLib = GLib
        self._icon_dir = os.path.abspath(icon_dir)
        self._on_action = on_action
        self._conn: Optional[Gio.DBusConnection] = None
        self._sni_reg_id = 0
        self._menu_reg_id = 0
        self._owner_id = 0
        self._available = False
        self._icon_name = "com.vpngate.client"
        self._tooltip = "VPN Gate Client - disconnected"
        self._menu_revision = 1
        self._flags = {"connect_enabled": True, "disconnect_enabled": False}

        self._setup()

    def is_available(self) -> bool:
        return self._available

    def _setup(self) -> None:
        Gio = self._Gio
        try:
            self._conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            if not self._conn:
                return

            sni_node = Gio.DBusNodeInfo.new_for_xml(SNI_XML)
            sni_iface = sni_node.interfaces[0]
            self._sni_reg_id = self._conn.register_object(
                object_path="/StatusNotifierItem",
                interface_info=sni_iface,
                method_call_closure=self._on_sni_method,
                get_property_closure=self._on_sni_get_property,
                set_property_closure=None,
            )

            menu_node = Gio.DBusNodeInfo.new_for_xml(DBUSMENU_XML)
            menu_iface = menu_node.interfaces[0]
            self._menu_reg_id = self._conn.register_object(
                object_path="/MenuBar",
                interface_info=menu_iface,
                method_call_closure=self._on_menu_method,
                get_property_closure=self._on_menu_get_property,
                set_property_closure=None,
            )

            bus_name = f"org.freedesktop.StatusNotifierItem-{os.getpid()}-1"
            self._owner_id = Gio.bus_own_name_on_connection(
                self._conn,
                bus_name,
                Gio.BusNameOwnerFlags.NONE,
                self._on_bus_acquired,
                None,
            )
            self._available = True
        except Exception as exc:
            self._available = False
            print(f"DBusTray init failed: {exc}", file=sys.stderr)

    def _on_bus_acquired(self, conn, name) -> None:
        Gio = self._Gio
        GLib = self._GLib
        try:
            watcher = Gio.DBusProxy.new_sync(
                conn,
                Gio.DBusProxyFlags.NONE,
                None,
                "org.kde.StatusNotifierWatcher",
                "/StatusNotifierWatcher",
                "org.kde.StatusNotifierWatcher",
                None,
            )
            watcher.call_sync(
                "RegisterStatusNotifierItem",
                GLib.Variant("(s)", ["/StatusNotifierItem"]),
                Gio.DBusCallFlags.NONE,
                1500,
                None,
            )
        except Exception:
            pass

    def _on_sni_get_property(self, conn, sender, path, iface, prop):
        GLib = self._GLib
        if prop == "Category":
            return GLib.Variant("s", "ApplicationStatus")
        elif prop == "Id":
            return GLib.Variant("s", "vpngate-client")
        elif prop == "Title":
            return GLib.Variant("s", "VPN Gate Client")
        elif prop == "Status":
            return GLib.Variant("s", "Active")
        elif prop == "WindowId":
            return GLib.Variant("i", 0)
        elif prop == "IconThemePath":
            return GLib.Variant("s", self._icon_dir)
        elif prop == "IconName":
            return GLib.Variant("s", self._icon_name)
        elif prop == "IconPixmap":
            return GLib.Variant("a(iiay)", [])
        elif prop == "OverlayIconName":
            return GLib.Variant("s", "")
        elif prop == "OverlayIconPixmap":
            return GLib.Variant("a(iiay)", [])
        elif prop == "AttentionIconName":
            return GLib.Variant("s", "")
        elif prop == "AttentionIconPixmap":
            return GLib.Variant("a(iiay)", [])
        elif prop == "AttentionMovieName":
            return GLib.Variant("s", "")
        elif prop == "ToolTip":
            return GLib.Variant("(sa(iiay)ss)", (self._icon_name, [], "VPN Gate Client", self._tooltip))
        elif prop == "ItemIsMenu":
            return GLib.Variant("b", False)
        elif prop == "Menu":
            return GLib.Variant("o", "/MenuBar")
        return None

    def _on_sni_method(self, conn, sender, path, iface, method, params, invocation) -> None:
        if method in ("Activate", "SecondaryActivate"):
            self._on_action("show")
            invocation.return_value(None)
        elif method == "ContextMenu":
            invocation.return_value(None)
        elif method == "Scroll":
            invocation.return_value(None)
        else:
            invocation.return_value(None)

    def _on_menu_get_property(self, conn, sender, path, iface, prop):
        GLib = self._GLib
        if prop == "Version":
            return GLib.Variant("u", 3)
        elif prop == "TextDirection":
            return GLib.Variant("s", "ltr")
        elif prop == "Status":
            return GLib.Variant("s", "normal")
        elif prop == "IconThemePath":
            return GLib.Variant("as", [self._icon_dir])
        return None

    def _on_menu_method(self, conn, sender, path, iface, method, params, invocation) -> None:
        GLib = self._GLib
        if method == "GetLayout":
            show_item = GLib.Variant(
                "(ia{sv}av)",
                (1, {"label": GLib.Variant("s", "Show VPN Gate"), "enabled": GLib.Variant("b", True), "visible": GLib.Variant("b", True)}, []),
            )
            connect_item = GLib.Variant(
                "(ia{sv}av)",
                (2, {"label": GLib.Variant("s", "Connect (recommended)"), "enabled": GLib.Variant("b", self._flags.get("connect_enabled", False)), "visible": GLib.Variant("b", True)}, []),
            )
            disconnect_item = GLib.Variant(
                "(ia{sv}av)",
                (3, {"label": GLib.Variant("s", "Disconnect"), "enabled": GLib.Variant("b", self._flags.get("disconnect_enabled", False)), "visible": GLib.Variant("b", True)}, []),
            )
            sep_item = GLib.Variant(
                "(ia{sv}av)",
                (4, {"type": GLib.Variant("s", "separator"), "visible": GLib.Variant("b", True)}, []),
            )
            quit_item = GLib.Variant(
                "(ia{sv}av)",
                (5, {"label": GLib.Variant("s", "Quit"), "enabled": GLib.Variant("b", True), "visible": GLib.Variant("b", True)}, []),
            )
            root_children = [
                GLib.Variant("v", show_item),
                GLib.Variant("v", connect_item),
                GLib.Variant("v", disconnect_item),
                GLib.Variant("v", sep_item),
                GLib.Variant("v", quit_item),
            ]
            root_layout = (0, {"children-display": GLib.Variant("s", "submenu")}, root_children)
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self._menu_revision, root_layout)))
        elif method == "GetGroupProperties":
            invocation.return_value(GLib.Variant("(a(ia{sv}))", ([])))
        elif method == "GetProperty":
            invocation.return_value(GLib.Variant("(v)", (GLib.Variant("s", ""),)))
        elif method == "Event":
            item_id = params[0]
            event_id = params[1]
            if event_id == "clicked":
                if item_id == 1:
                    self._on_action("show")
                elif item_id == 2:
                    self._on_action("connect")
                elif item_id == 3:
                    self._on_action("disconnect")
                elif item_id == 5:
                    self._on_action("quit")
            invocation.return_value(None)
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        else:
            invocation.return_value(None)

    def set_state(self, status: str) -> None:
        mapping = {
            "idle": "VPN Gate Client - disconnected",
            "authorizing": "VPN Gate Client - requesting privileges ...",
            "connecting": "VPN Gate Client - connecting ...",
            "connected": "VPN Gate Client - connected",
            "error": "VPN Gate Client - error",
        }
        self._icon_name = "com.vpngate.client"
        self._tooltip = mapping.get(status, mapping["idle"])
        if self._conn:
            try:
                self._conn.emit_signal(
                    None,
                    "/StatusNotifierItem",
                    "org.kde.StatusNotifierItem",
                    "NewIcon",
                    None,
                )
                self._conn.emit_signal(
                    None,
                    "/StatusNotifierItem",
                    "org.kde.StatusNotifierItem",
                    "NewToolTip",
                    None,
                )
            except Exception:
                pass

    def set_menu(self, **flags: bool) -> None:
        self._flags.update(flags)
        self._menu_revision += 1
        if self._conn:
            try:
                self._conn.emit_signal(
                    None,
                    "/MenuBar",
                    "com.canonical.dbusmenu",
                    "LayoutUpdated",
                    self._GLib.Variant("(ui)", (self._menu_revision, 0)),
                )
            except Exception:
                pass

    def notify(self, title: str, message: str, level: str = "info") -> bool:
        if self._conn:
            try:
                urgency = 2 if level in ("warning", "error", "alarm") else 1
                hints = {
                    "urgency": self._GLib.Variant("y", urgency),
                }
                parameters = self._GLib.Variant(
                    "(susssasa{sv}i)",
                    (
                        "VPN Gate Client",
                        0,
                        "com.vpngate.client",
                        title,
                        message,
                        [],
                        hints,
                        7000 if urgency == 2 else 5000,
                    ),
                )
                self._conn.call(
                    "org.freedesktop.Notifications",
                    "/org/freedesktop/Notifications",
                    "org.freedesktop.Notifications",
                    "Notify",
                    parameters,
                    None,
                    self._Gio.DBusCallFlags.NONE,
                    1500,
                    None,
                    None,
                )
                return True
            except Exception:
                pass
        return False

    def stop(self) -> None:
        if self._conn:
            try:
                if self._sni_reg_id:
                    self._conn.unregister_object(self._sni_reg_id)
                    self._sni_reg_id = 0
                if self._menu_reg_id:
                    self._conn.unregister_object(self._menu_reg_id)
                    self._menu_reg_id = 0
            except Exception:
                pass
        if self._owner_id:
            try:
                self._Gio.bus_unown_name(self._owner_id)
                self._owner_id = 0
            except Exception:
                pass
        self._available = False


# ============================================================================
# Legacy Helper Process Tray (AppIndicator3 fallback)
# ============================================================================

class _ProcessTray:
    def __init__(self, icon_dir: str, on_action: Callable[[str], None]) -> None:
        self._icon_dir = icon_dir
        self._on_action = on_action
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._state = {"icon": "com.vpngate.client"}
        self._start()

    def is_available(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

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
                start_new_session=False,
            )
        except OSError:
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
        for line in self._proc.stdout:
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
                except Exception:
                    pass
        self._proc = None

    def set_state(self, status: str) -> None:
        mapping = {
            "idle": "VPN Gate Client - disconnected",
            "authorizing": "VPN Gate Client - requesting privileges ...",
            "connecting": "VPN Gate Client - connecting ...",
            "connected": "VPN Gate Client - connected",
            "error": "VPN Gate Client - error",
        }
        text = mapping.get(status, mapping["idle"])
        self._state = {"icon": "com.vpngate.client", "tooltip": text}
        self._send(self._state)

    def set_menu(self, **flags: bool) -> None:
        self._send({"event": "menu", **flags})

    def notify(self, title: str, message: str, level: str = "info") -> bool:
        self._send({"event": "notify", "title": title, "message": message, "level": level})
        return True

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass


# ============================================================================
# Main Public Tray Wrapper
# ============================================================================

class Tray:
    """Owns the platform-appropriate system tray implementation."""

    def __init__(self, icon_dir: str, on_action: Callable[[str], None]) -> None:
        self._backend = None
        if sys.platform == "win32":
            try:
                self._backend = _Win32Tray(icon_dir, on_action)
            except Exception as exc:
                print(f"Win32 tray initialization failed: {exc}", file=sys.stderr)
        else:
            try:
                backend = _DBusTray(icon_dir, on_action)
                if backend.is_available():
                    self._backend = backend
            except Exception as exc:
                print(f"DBus tray initialization failed: {exc}", file=sys.stderr)

            if self._backend is None:
                try:
                    backend = _ProcessTray(icon_dir, on_action)
                    if backend.is_available():
                        self._backend = backend
                except Exception as exc:
                    print(f"Process tray initialization failed: {exc}", file=sys.stderr)

    def is_available(self) -> bool:
        return self._backend is not None and self._backend.is_available()

    def set_state(self, status: str) -> None:
        if self._backend:
            self._backend.set_state(status)

    def set_menu(self, **flags: bool) -> None:
        if self._backend:
            self._backend.set_menu(**flags)

    def notify(self, title: str, message: str, level: str = "info") -> bool:
        if self._backend and hasattr(self._backend, "notify"):
            return self._backend.notify(title, message, level)
        return False

    def stop(self) -> None:
        if self._backend:
            self._backend.stop()
            self._backend = None

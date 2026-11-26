"""Automated tests for concurrency, race conditions, deadlocks, process management,
and user interaction edge cases across the VPN Gate client codebase.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from unittest import mock

import pytest

# Ensure app package is in path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP_DIR = os.path.join(ROOT_DIR, "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# Mock gi and cairo before importing UI if not present
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

from vpngate.backends import (
    BACKENDS,
    BackendError,
    L2tpBackend,
    OpenVpnBackend,
    SoftEtherBackend,
    SstpBackend,
    _claim_ras_profile_owner,
    _claim_ras_route_owner,
    _cleanup_windows_ras,
)
from vpngate.models import Protocol, VpnServer
from vpngate.tray import _Win32Tray
from vpngate.ui import VpnGateWindow
from vpngate.worker import Worker


# ============================================================================
# 1. Worker Concurrency, Deadlocks & Attempt ID Invalidation
# ============================================================================

class TestWorkerEdgeCases:
    def test_worker_lock_is_rlock(self):
        """Worker._connect_lock must be an RLock to allow re-entrant operations."""
        worker = Worker()
        assert isinstance(worker._connect_lock, type(threading.RLock()))
        # Verify re-entrancy
        with worker._connect_lock:
            with worker._connect_lock:
                pass

    def test_worker_no_deadlock_on_reconnect_while_connected(self):
        """start_connect when already connected must cleanly disconnect without deadlocking.

        Also verifies that emit_state=False is passed so state: disconnected
        is not emitted mid-reconnect.
        """
        worker = Worker()
        mock_backend = mock.MagicMock()
        mock_backend.available.return_value = True

        worker.connection = {"server": "old_relay", "process": None}
        worker.backend = mock_backend
        worker.protocol = Protocol.OPENVPN

        with mock.patch("vpngate.worker.BACKENDS", {Protocol.OPENVPN: mock_backend}), \
             mock.patch("vpngate.worker._emit") as mock_emit:
            # Dial new server while old connection is present
            command = {
                "cmd": "connect",
                "protocol": "openvpn",
                "server": {"hostname": "new_relay", "ip": "1.2.3.4"},
                "timeout": 5,
                "retries": 1,
            }
            # This must complete immediately without deadlocking
            worker.start_connect(command)

            # Old backend must have been disconnected cleanly
            mock_backend.disconnect.assert_called_once()
            # Must not emit state: disconnected during reconnect
            emitted_states = [call[0][0] for call in mock_emit.call_args_list if call[0][0].get("event") == "state"]
            disconnected_emits = [s for s in emitted_states if s.get("state") == "disconnected"]
            assert len(disconnected_emits) == 0, f"Unexpected disconnected state emitted: {disconnected_emits}"
            assert any(s.get("state") == "connecting" for s in emitted_states)

    def test_worker_attempt_id_invalidation_of_stale_success(self):
        """If an old connect worker succeeds after being superseded, it must be discarded."""
        worker = Worker()
        mock_backend = mock.MagicMock()
        mock_backend.name = "MockBackend"
        server = VpnServer()
        server.hostname = "test_relay"
        server.ip = "1.2.3.4"

        # Simulate attempt 1 being started
        worker._is_connecting = True
        worker._connecting_backend = mock_backend
        worker._active_attempt_id = 1
        stale_attempt_id = 1

        # While attempt 1 is connecting, a new attempt or abort invalidates it
        worker._active_attempt_id = 2

        with mock.patch("vpngate.worker._emit") as mock_emit:
            # Attempt 1 returns successfully
            info = {"server": "test_relay", "interface": "tun0"}
            mock_backend.connect.return_value = info

            worker._connect_thread_worker(mock_backend, server, Protocol.OPENVPN, 10, 1, stale_attempt_id)

            # Because attempt_id 1 was invalidated:
            # 1. Backend disconnect must have been called on the stale info
            mock_backend.disconnect.assert_called_once_with(info, worker._log)
            # 2. Worker connection must NOT be set
            assert worker.connection is None
            # 3. No "connected" event should have been emitted
            emitted_events = [call[0][0].get("event") for call in mock_emit.call_args_list]
            assert "connected" not in emitted_events

    def test_worker_attempt_id_invalidation_of_stale_error(self):
        """If an old connect worker fails with BackendError after being superseded, ignore it."""
        worker = Worker()
        mock_backend = mock.MagicMock()
        server = VpnServer()

        worker._is_connecting = True
        worker._connecting_backend = mock_backend
        worker._active_attempt_id = 2  # Superseded
        stale_attempt_id = 1

        with mock.patch("vpngate.worker._emit") as mock_emit:
            mock_backend.connect.side_effect = BackendError("Connection timed out")
            worker._connect_thread_worker(mock_backend, server, Protocol.OPENVPN, 10, 1, stale_attempt_id)

            # Nothing should be emitted for the stale attempt
            emitted_events = [call[0][0].get("event") for call in mock_emit.call_args_list]
            assert "error" not in emitted_events
            assert "state" not in emitted_events

    def test_worker_do_abort_or_disconnect_invalidates_in_flight_thread(self):
        """do_abort_or_disconnect increments attempt ID, aborts backend, and joins thread."""
        worker = Worker()
        mock_backend = mock.MagicMock()
        worker._is_connecting = True
        worker._connecting_backend = mock_backend
        worker._active_attempt_id = 5

        # Create a mock thread that simulates slow in-flight connect
        mock_thread = mock.MagicMock()
        mock_thread.is_alive.return_value = True
        worker._connect_thread = mock_thread

        with mock.patch("vpngate.worker._emit") as mock_emit:
            worker.do_abort_or_disconnect()

            # Attempt ID must be incremented
            assert worker._active_attempt_id == 6
            # Connecting state cleared
            assert not worker._is_connecting
            assert worker._connecting_backend is None
            # Backend abort called
            mock_backend.abort.assert_called_once()
            # Connect thread joined with timeout
            mock_thread.join.assert_called_once_with(timeout=2.0)
            # Cancelled state emitted
            cancelled = [call[0][0] for call in mock_emit.call_args_list if call[0][0].get("message") == "Connection cancelled."]
            assert len(cancelled) == 1
            # Exactly one state: disconnected event must be emitted (no duplicate)
            disconnected_states = [call[0][0] for call in mock_emit.call_args_list if call[0][0].get("event") == "state" and call[0][0].get("state") == "disconnected"]
            assert len(disconnected_states) == 1

    def test_worker_invalidation_during_external_ip_resolution(self):
        """If worker is aborted or superseded while netutil.external_ip() is running, discard and disconnect."""
        worker = Worker()
        mock_backend = mock.MagicMock()
        mock_backend.name = "MockBackend"
        server = VpnServer()
        server.hostname = "test_relay"
        server.ip = "1.2.3.4"

        attempt_id = 1
        worker._is_connecting = True
        worker._connecting_backend = mock_backend
        worker._active_attempt_id = 1

        info = {"server": "test_relay", "interface": "tun0"}
        mock_backend.connect.return_value = info

        def slow_external_ip():
            # While external_ip is resolving, user presses abort / disconnect
            worker.do_abort_or_disconnect()
            return "8.8.8.8"

        with mock.patch("vpngate.netutil.external_ip", side_effect=slow_external_ip), \
             mock.patch("vpngate.worker._emit") as mock_emit:
            worker._connect_thread_worker(mock_backend, server, Protocol.OPENVPN, 10, 1, attempt_id)

            # Must have cleanly disconnected the info
            assert mock_backend.disconnect.called
            # Must NOT emit connected event
            emitted_events = [call[0][0].get("event") for call in mock_emit.call_args_list]
            assert "connected" not in emitted_events
            # Final state in worker must not be connected
            assert worker.connection is None

    def test_worker_uses_fresh_backend_instance_per_attempt(self):
        """Each start_connect must allocate an isolated Backend instance so old threads cannot tamper with _abort_requested."""
        worker = Worker()
        template = OpenVpnBackend()
        captured_backends = []

        def fake_thread_worker(backend, *args, **kwargs):
            captured_backends.append(backend)

        with mock.patch("vpngate.worker.BACKENDS", {Protocol.OPENVPN: template}), \
             mock.patch.object(OpenVpnBackend, "available", return_value=True), \
             mock.patch.object(worker, "_connect_thread_worker", side_effect=fake_thread_worker), \
             mock.patch("vpngate.worker._emit"):
            worker.start_connect({"cmd": "connect", "protocol": "openvpn", "server": {"hostname": "r1", "ip": "1.1.1.1"}, "attempt_id": 1})
            time.sleep(0.05)
            worker.start_connect({"cmd": "connect", "protocol": "openvpn", "server": {"hostname": "r2", "ip": "2.2.2.2"}, "attempt_id": 2})
            time.sleep(0.05)

        assert len(captured_backends) == 2
        b1, b2 = captured_backends
        assert b1 is not b2
        assert b1 is not template
        b1._abort_requested = True
        assert b2._abort_requested is False

    def test_worker_delayed_monitor_disconnect_does_not_emit_after_new_connect(self):
        """If monitor do_disconnect runs slowly while a new attempt starts, it must not emit stale state: disconnected."""
        worker = Worker()
        old_backend = mock.MagicMock()
        conn1 = {"server": "old_relay", "process": None, "_client_attempt_id": 1}
        worker.connection = conn1
        worker.backend = old_backend
        worker.protocol = Protocol.OPENVPN
        worker._active_attempt_id = 1

        def slow_disconnect(conn, log_fn):
            # Simulate a new connection attempt starting while old backend.disconnect is still cleaning up
            with worker._connect_lock:
                worker._active_attempt_id += 1
                worker._is_connecting = True

        old_backend.disconnect.side_effect = slow_disconnect

        with mock.patch("vpngate.worker._emit") as mock_emit:
            worker.do_disconnect(emit_state=True, expected_conn=conn1)
            disconnected_emits = [
                call[0][0] for call in mock_emit.call_args_list
                if call[0][0].get("event") == "state" and call[0][0].get("state") == "disconnected"
            ]
            assert len(disconnected_emits) == 0

    def test_worker_ignores_stale_abort_command_with_older_attempt_id(self):
        """Worker must ignore abort/disconnect commands whose attempt_id is older than active _client_attempt_id."""
        worker = Worker()
        mock_backend = mock.MagicMock()
        mock_backend.available.return_value = True
        worker._is_connecting = True
        worker._connecting_backend = mock_backend
        worker._active_attempt_id = 5
        worker._client_attempt_id = 5

        with mock.patch("vpngate.worker._emit"):
            worker.handle({"cmd": "abort", "attempt_id": 4})

        # Active attempt 5 must NOT have been aborted
        mock_backend.abort.assert_not_called()
        assert worker._is_connecting is True
        assert worker._active_attempt_id == 5


# ============================================================================
# 2. UI Edge Cases: Auto-Reconnect, Abort Button, Tray Actions & Latch
# ============================================================================

class TestUiEdgeCases:
    def _create_mock_window(self):
        win = object.__new__(VpnGateWindow)
        win.state = "idle"
        win.settings = {"auto_reconnect": True, "auto_reconnect_retries": 3, "auto_reconnect_delay": 2}
        win._aborting = False
        win._manual_disconnect = False
        win._notified_connected = False
        win._tunnel_dropped_from_connected = False
        win._auto_reconnecting = False
        win._auto_reconnect_attempt = 0
        win._auto_reconnect_timer_id = None
        win._client_attempt_id = 0
        win._connection_epoch = 0
        win._watchdog_in_flight = False
        win._watchdog_last_check = 0.0
        win._watchdog_failures = 0
        win._has_logged_first_probe = False
        win._last_connectivity_log_time = 0.0
        win.connectivity_targets = ["https://1.1.1.1"]
        win.connectivity_target_states = {"https://1.1.1.1": True}
        win.connected_info = None
        win._spawning_worker = False
        win._pending = None
        win._sudo_password = None
        win._protocol_choice = {}

        win.selected = mock.MagicMock()
        win.recommended = None
        win.worker = mock.MagicMock()
        win.worker.poll.return_value = None
        win.connect_btn = mock.MagicMock()
        win.connect_btn.props = mock.MagicMock()
        win.hero_ring = mock.MagicMock()
        win.hero_title = mock.MagicMock()
        win.hero_sub = mock.MagicMock()
        win.header_spinner = mock.MagicMock()
        win.tray = mock.MagicMock()
        win.title_widget = mock.MagicMock()
        win.title_widget.props = mock.MagicMock()
        for chip_name in ("chip_server", "chip_tunnel", "chip_public", "chip_iface", "chip_uptime"):
            chip = mock.MagicMock()
            chip.props = mock.MagicMock()
            setattr(win, chip_name, chip)
        win._auth_dialog = None

        win._hide_chips = mock.MagicMock()
        win._append_log = mock.MagicMock()
        win.toast = mock.MagicMock()
        win.notify_system = mock.MagicMock()
        win._send = mock.MagicMock()
        win._set_state = mock.MagicMock()
        return win

    def test_connect_unconditionally_cancels_auto_reconnect(self):
        """_connect() must cancel any scheduled auto-reconnect and reset attempt counter."""
        win = self._create_mock_window()
        win._auto_reconnect_attempt = 3
        win._auto_reconnect_timer_id = 99

        server = VpnServer()
        server.openvpn_udp = 1194

        win._connect(server, Protocol.OPENVPN)

        assert win._auto_reconnect_attempt == 0
        assert win._auto_reconnect_timer_id is None

    def test_abort_connect_sets_stopping_and_disables_button(self):
        """_abort_connect() must immediately disable connect_btn and set label to 'Stopping …'."""
        win = self._create_mock_window()
        win.state = "connecting"
        win._auto_reconnect_attempt = 2

        win._abort_connect()

        assert win._aborting is True
        assert win._auto_reconnect_attempt == 0
        win.connect_btn.set_sensitive.assert_called_with(False)
        assert win.connect_btn.props.label == "Stopping …"
        win._send.assert_called_with({"cmd": "abort"})

    def test_can_connect_and_update_buttons_while_aborting(self):
        """When _aborting is True, _can_connect must return False and button must stay disabled."""
        win = self._create_mock_window()
        win.state = "idle"
        win._aborting = True

        assert win._can_connect() is False

        win._update_buttons()
        assert win.connect_btn.props.label == "Stopping …"
        win.connect_btn.set_sensitive.assert_called_with(False)

    def test_dial_failure_does_not_trigger_auto_reconnect(self):
        """When dialing fails before VPN was established, auto-reconnect must NOT be scheduled."""
        win = self._create_mock_window()
        win.state = "connecting"
        win._notified_connected = False
        win._tunnel_dropped_from_connected = False

        with mock.patch.object(win, "_schedule_auto_reconnect") as mock_sched:
            # Simulate worker emitting disconnected state after dial failure
            win._handle_state({"state": "disconnected"})
            mock_sched.assert_not_called()

    def test_tunnel_drop_after_connected_triggers_auto_reconnect_via_latch(self):
        """When an established tunnel drops, the error latch preserves was_connected for auto-reconnect."""
        win = self._create_mock_window()
        win.state = "connected"
        win._notified_connected = True

        # Simulate error event from worker on tunnel process drop
        event = {"event": "error", "message": "VPN tunnel process stopped"}
        win._dispatch_event(event)

        # Latch must be set
        assert win._tunnel_dropped_from_connected is True
        assert win.state == "error"

        with mock.patch.object(win, "_schedule_auto_reconnect") as mock_sched:
            # Worker follows up with state: disconnected
            win._handle_state({"state": "disconnected"})
            # Latch must have allowed auto-reconnect to be scheduled
            mock_sched.assert_called_once()
            # Latch must be reset
            assert win._tunnel_dropped_from_connected is False

    def test_manual_disconnect_does_not_trigger_auto_reconnect(self):
        """Manual disconnect must never trigger auto-reconnect."""
        win = self._create_mock_window()
        win.state = "connected"
        win._notified_connected = True
        win._manual_disconnect = True

        with mock.patch.object(win, "_schedule_auto_reconnect") as mock_sched:
            win._handle_state({"state": "disconnected"})
            mock_sched.assert_not_called()

    def test_tray_action_disconnect_aborts_or_disconnects(self):
        """Tray 'disconnect' must abort during dialing and disconnect when connected."""
        win = self._create_mock_window()

        # 1. Connecting state -> abort
        win.state = "connecting"
        win._abort_connect = mock.MagicMock()
        with mock.patch("gi.repository.GLib.idle_add", side_effect=lambda fn, *a: fn(*a)):
            win._on_tray_action("disconnect")
            win._abort_connect.assert_called_once()

        # 2. Connected state -> disconnect
        win.state = "connected"
        win._disconnect = mock.MagicMock()
        with mock.patch("gi.repository.GLib.idle_add", side_effect=lambda fn, *a: fn(*a)):
            win._on_tray_action("disconnect")
            win._disconnect.assert_called_once()

    def test_worker_spawning_concurrency_guard_and_kill_on_abort(self):
        """Spawning worker must prevent concurrent spawns and kill newly spawned worker if aborted."""
        win = self._create_mock_window()
        win.worker = None
        win._spawning_worker = False

        # 1. Start authorization sets _spawning_worker = True
        with mock.patch("threading.Thread") as mock_thread:
            win._start_authorization()
            assert win._spawning_worker is True
            assert mock_thread.called

            # Second call while spawning must return immediately
            mock_thread.reset_mock()
            win._start_authorization()
            mock_thread.assert_not_called()

        # 2. While spawning is in flight, user clicks Stop (_abort_connect)
        win.state = "authorizing"
        win._abort_connect()
        # _aborting must remain True because worker was spawning!
        assert win._aborting is True

        # 3. Background thread completes spawn and calls _worker_started
        mock_proc = mock.MagicMock()
        with mock.patch.object(win, "_update_buttons") as mock_update:
            win._worker_started(mock_proc, "windows")

            assert win._spawning_worker is False
            assert win._aborting is False
            assert win.state == "idle"
            mock_proc.kill.assert_called_once()
            mock_update.assert_called_once()

    def test_auto_reconnect_multi_attempt_retry_loop(self):
        """Verify auto-reconnect maintains retry counter across multiple failed dials up to max_attempts."""
        win = self._create_mock_window()
        win.state = "connected"
        win._notified_connected = True
        win.settings["auto_reconnect_retries"] = 3

        # Drop 1: initial drop from connected
        win._dispatch_event({"event": "error", "message": "Tunnel dropped"})
        assert win._tunnel_dropped_from_connected is True

        # Simulate timeout add to directly capture callback
        scheduled_callbacks = []
        def mock_timeout_add(delay, cb):
            scheduled_callbacks.append(cb)
            return 100 + len(scheduled_callbacks)

        with mock.patch("gi.repository.GLib.timeout_add_seconds", side_effect=mock_timeout_add):
            # Worker reports disconnected
            win._handle_state({"state": "disconnected"})
            assert win._auto_reconnecting is True
            assert win._auto_reconnect_attempt == 1
            assert len(scheduled_callbacks) == 1

            # Fire reconnect attempt 1
            scheduled_callbacks.pop(0)()
            assert win._auto_reconnect_attempt == 1  # Not reset to 0!

            # Attempt 1 fails: worker sends error + disconnected
            win._dispatch_event({"event": "error", "message": "Dial 1 failed"})
            win._handle_state({"state": "disconnected"})
            assert win._auto_reconnecting is True
            assert win._auto_reconnect_attempt == 2
            assert len(scheduled_callbacks) == 1

            # Fire reconnect attempt 2
            scheduled_callbacks.pop(0)()
            assert win._auto_reconnect_attempt == 2

            # Attempt 2 fails
            win._dispatch_event({"event": "error", "message": "Dial 2 failed"})
            win._handle_state({"state": "disconnected"})
            assert win._auto_reconnecting is True
            assert win._auto_reconnect_attempt == 3
            assert len(scheduled_callbacks) == 1

            # Fire reconnect attempt 3
            scheduled_callbacks.pop(0)()
            assert win._auto_reconnect_attempt == 3

            # Attempt 3 fails -> reaches max_attempts
            win._dispatch_event({"event": "error", "message": "Dial 3 failed"})
            win._handle_state({"state": "disconnected"})
            # Must stop auto-reconnecting
            assert win._auto_reconnecting is False
            assert win._auto_reconnect_attempt == 0

    def test_auto_reconnect_countdown_shows_stop_button_and_allows_abort(self):
        """During auto-reconnect countdown, connect_btn must show 'Stop', _can_connect() is False, and clicking cancels it."""
        win = self._create_mock_window()
        win.state = "connected"
        win._notified_connected = True
        win._dispatch_event({"event": "error", "message": "Drop"})

        scheduled_callbacks = []
        with mock.patch("gi.repository.GLib.timeout_add_seconds", side_effect=lambda d, cb: (scheduled_callbacks.append(cb), 123)[1]):
            win._handle_state({"state": "disconnected"})

        assert win._auto_reconnecting is True
        assert win._can_connect() is False
        assert win.connect_btn.props.label == "Stop"
        win.connect_btn.set_sensitive.assert_called_with(True)
        win.header_spinner.set_visible.assert_called_with(True)
        win.tray.set_menu.assert_called_with(connect_enabled=False, disconnect_enabled=True)

        # User clicks primary button (currently labeled "Stop") during countdown
        win._on_connect_clicked()

        assert win._auto_reconnecting is False
        assert win._auto_reconnect_timer_id is None
        assert win._auto_reconnect_attempt == 0
        assert win.state == "idle"
        assert win.connect_btn.props.label == "Connect"
        win._set_state.assert_called_with("idle", "Connection attempt stopped.")

    def test_stale_attempt_events_ignored_after_new_connect(self):
        """Stale aborted/failed events from an older attempt_id must not trigger error/disconnected after new connect."""
        win = self._create_mock_window()
        server = VpnServer()
        server.openvpn_udp = 1194

        # Connect attempt 1
        win._connect(server, Protocol.OPENVPN)
        aid1 = win._client_attempt_id
        assert aid1 == 1

        # Connect attempt 2 immediately
        win._connect(server, Protocol.OPENVPN)
        aid2 = win._client_attempt_id
        assert aid2 == 2

        # Attempt 2 establishes connection
        info2 = {"server": "relay2", "ip": "2.2.2.2", "protocol": "openvpn"}
        win._dispatch_event({"event": "connected", "info": info2, "attempt_id": aid2})
        win._dispatch_event({"event": "state", "state": "connected", "attempt_id": aid2})
        assert win.state == "connected"
        win.notify_system.reset_mock()
        win.toast.reset_mock()

        # Late events from Attempt 1 arrive ("Connection aborted by user." + "disconnected")
        win._dispatch_event({"event": "error", "message": "Connection aborted by user.", "attempt_id": aid1})
        win._dispatch_event({"event": "state", "state": "disconnected", "attempt_id": aid1})

        # Must remain connected with zero error/disconnect notifications!
        assert win.state == "connected"
        assert win.connected_info is info2
        win.notify_system.assert_not_called()
        win.toast.assert_not_called()

    def test_stale_watchdog_probe_ignored_after_session_change(self):
        """A slow watchdog probe from Session 1 failing after Session 2 connects must not disconnect Session 2."""
        win = self._create_mock_window()
        win.state = "connected"
        win._notified_connected = True
        win.settings["watchdog_threshold"] = 1
        win._handle_connected({"server": "relay1", "ip": "1.1.1.1"})

        captured_probes = []
        with mock.patch("vpngate.ui.probe_target", return_value=False), \
             mock.patch("gi.repository.GLib.idle_add", side_effect=lambda fn, *a: captured_probes.append(fn)):
            win._run_watchdog_probe(win._connection_epoch)

        # Session 1 drops and Session 2 connects cleanly before Session 1's probe result is processed!
        win._handle_connected({"server": "relay2", "ip": "2.2.2.2"})
        assert win._watchdog_failures == 0

        # Deliver stale probe completion from Session 1
        assert len(captured_probes) == 1
        captured_probes[0]()

        # Session 2 must stay completely unaffected
        assert win.state == "connected"
        assert win._watchdog_failures == 0
        win._send.assert_not_called()
        win.toast.assert_not_called()


# ============================================================================
# 3. Backends: Process Termination, Safe File Writes & RAS Cleanup
# ============================================================================

class TestBackendsEdgeCases:
    def test_openvpn_terminate_waits_and_closes_streams(self):
        """OpenVpnBackend._terminate on Windows waits for exit and closes stdout/stderr/stdin."""
        mock_proc = mock.MagicMock()
        mock_proc.pid = 1234
        mock_proc.stdout = mock.MagicMock()
        mock_proc.stderr = mock.MagicMock()
        mock_proc.stdin = mock.MagicMock()

        with mock.patch("sys.platform", "win32"), \
             mock.patch("subprocess.run") as mock_run:
            OpenVpnBackend._terminate(mock_proc)

            mock_run.assert_called_once()
            # Must call proc.wait(timeout=3)
            mock_proc.wait.assert_called_once_with(timeout=3)
            # Must close all streams
            mock_proc.stdout.close.assert_called_once()
            mock_proc.stderr.close.assert_called_once()
            mock_proc.stdin.close.assert_called_once()

    def test_openvpn_safe_write_file_retries_transient_permission_error(self, tmp_path):
        """_safe_write_file retries on PermissionError/OSError and succeeds."""
        target_file = str(tmp_path / "test_auth.txt")
        call_count = 0

        original_open = open

        def flaky_open(file, mode="r", *args, **kwargs):
            nonlocal call_count
            if file == target_file and "w" in mode and call_count < 2:
                call_count += 1
                raise PermissionError("Access is denied (transient lock)")
            return original_open(file, mode, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=flaky_open), \
             mock.patch("time.sleep") as mock_sleep:
            OpenVpnBackend._safe_write_file(target_file, "user\npass\n")

            assert call_count == 2
            assert mock_sleep.call_count == 2
            with open(target_file) as f:
                assert f.read() == "user\npass\n"

    def test_openvpn_connect_rejects_missing_or_directory_binary(self, tmp_path):
        """OpenVpnBackend.connect raises BackendError when openvpn_bin is missing or is a directory."""
        backend = OpenVpnBackend()
        server = VpnServer()

        # 1. Directory instead of executable
        dir_path = str(tmp_path / "fake_dir")
        os.makedirs(dir_path, exist_ok=True)
        with mock.patch.object(backend, "_openvpn_bin", return_value=dir_path):
            with pytest.raises(BackendError, match="OpenVPN executable not found"):
                backend.connect(server, Protocol.OPENVPN, mock.MagicMock(), str(tmp_path))

        # 2. Non-existent file
        with mock.patch.object(backend, "_openvpn_bin", return_value=str(tmp_path / "non_existent.exe")):
            with pytest.raises(BackendError, match="OpenVPN executable not found"):
                backend.connect(server, Protocol.OPENVPN, mock.MagicMock(), str(tmp_path))

    def test_softether_run_commands_unique_files_and_cleanup(self, tmp_path):
        """SoftEtherBackend._run_commands uses unique filenames and removes them afterwards."""
        created_files = []

        original_run = subprocess.run

        def mock_subproc_run(cmd, *args, **kwargs):
            # Extract IN and OUT files
            in_arg = next(arg for arg in cmd if arg.startswith("/IN:"))
            out_arg = next(arg for arg in cmd if arg.startswith("/OUT:"))
            cmd_file = in_arg.split(":", 1)[1]
            out_file = out_arg.split(":", 1)[1]
            created_files.extend([cmd_file, out_file])
            assert "vpncmd_" in cmd_file
            assert "vpncmd_" in out_file
            # Simulate output
            with open(out_file, "w") as f:
                f.write("AccountList command completed.\n")
            return mock.MagicMock(returncode=0)

        with mock.patch("vpngate.backends.STATE_DIR", str(tmp_path)), \
             mock.patch("vpngate.backends.self_cmd", return_value="vpncmd"), \
             mock.patch("subprocess.run", side_effect=mock_subproc_run):
            output = SoftEtherBackend._run_commands(["AccountList"])
            assert "AccountList command completed." in output

            # Temp files must be cleaned up
            for f in created_files:
                assert not os.path.exists(f), f"Temporary file {f} was not cleaned up!"

    def test_cleanup_windows_ras_terminates_specific_pid_and_preserves_other_profiles(self):
        """_cleanup_windows_ras kills only specific PID when provided and only cleans specified vpn_name."""
        mock_proc = mock.MagicMock()
        mock_proc.pid = 9876
        mock_proc.poll.return_value = None

        with mock.patch("sys.platform", "win32"), \
             mock.patch("subprocess.run") as mock_run, \
             mock.patch("vpngate.backends._remove_windows_vpn_pbk_entry") as mock_pbk:
            _cleanup_windows_ras("VPNGate", active_proc=mock_proc)

            # Check all subprocess calls
            cmd_args = [call[0][0] for call in mock_run.call_args_list]

            # 1. Must NOT blindly taskkill all rasdial.exe
            assert not any(cmd[0] == "taskkill" and "/IM" in cmd and "rasdial.exe" in cmd for cmd in cmd_args)
            # 2. Must kill the specific PID
            assert any(cmd[0] == "taskkill" and "/PID" in cmd and "9876" in cmd for cmd in cmd_args)
            # 3. Must only clean up "VPNGate", not "VPNGate2"
            mock_pbk.assert_called_once_with("VPNGate")

    def test_sstp_and_l2tp_abort_terminates_active_powershell_proc(self):
        """SstpBackend and L2tpBackend abort() terminates tracked background configuration process."""
        mock_ps_proc = mock.MagicMock()
        mock_ps_proc.poll.return_value = None

        # Test SSTP
        sstp = SstpBackend()
        sstp._active_proc = mock_ps_proc
        with mock.patch("sys.platform", "win32"), mock.patch("subprocess.run"):
            sstp.abort(mock.MagicMock())
            mock_ps_proc.terminate.assert_called_once()
            mock_ps_proc.kill.assert_called_once()
            assert sstp._active_proc is None

        # Test L2TP
        mock_ps_proc.reset_mock()
        mock_ps_proc.poll.return_value = None
        l2tp = L2tpBackend()
        l2tp._active_proc = mock_ps_proc
        with mock.patch("sys.platform", "win32"), mock.patch("subprocess.run"):
            l2tp.abort(mock.MagicMock())
            mock_ps_proc.terminate.assert_called_once()
            mock_ps_proc.kill.assert_called_once()
            assert l2tp._active_proc is None

    def test_softether_connect_windows_honors_abort_in_polling_loops(self):
        """SoftEtherBackend._connect_windows raises BackendError immediately when _abort_requested is True."""
        backend = SoftEtherBackend()
        server = VpnServer()
        server.hostname = "test_softether"
        server.ip = "1.2.3.4"

        server.ssl_vpn_port = 443
        backend._abort_requested = True
        with mock.patch.object(backend, "_run_commands") as mock_run:
            with pytest.raises(BackendError, match="Connection aborted by user"):
                backend._connect_windows(server, Protocol.SSLVP, mock.MagicMock(), "/tmp")
            # Must run disconnect command to clean up
            assert any("AccountDisconnect" in call[0][0][0] for call in mock_run.call_args_list)

    def test_sstp_and_l2tp_connect_windows_honors_abort_before_rasdial(self):
        """SstpBackend and L2tpBackend raise BackendError immediately if aborted before dialing."""
        server = VpnServer()
        server.hostname = "test_ras"
        server.ip = "5.6.7.8"

        mock_ps = mock.MagicMock()
        mock_ps.communicate.return_value = ("", "")
        mock_ps.returncode = 0

        # 1. SSTP
        sstp = SstpBackend()
        sstp._abort_requested = True
        with mock.patch("sys.platform", "win32"), \
             mock.patch("subprocess.Popen", return_value=mock_ps), \
             mock.patch.object(SstpBackend, "_configure_windows_vpn_pbk"), \
             mock.patch("vpngate.backends._cleanup_windows_ras") as mock_clean:
            with pytest.raises(BackendError, match="Connection aborted by user"):
                sstp._connect_windows(server, Protocol.SSTP, mock.MagicMock(), "/tmp", 30, 1)
            assert mock_clean.called

        # 2. L2TP
        l2tp = L2tpBackend()
        l2tp._abort_requested = True
        with mock.patch("sys.platform", "win32"), \
             mock.patch("subprocess.Popen", return_value=mock_ps), \
             mock.patch.object(SstpBackend, "_configure_windows_vpn_pbk"), \
             mock.patch("vpngate.backends._cleanup_windows_ras") as mock_clean_l2tp:
            with pytest.raises(BackendError, match="Connection aborted by user"):
                l2tp._connect_windows(server, Protocol.L2TP, mock.MagicMock(), "/tmp", 30, 1)
            assert mock_clean_l2tp.called

    def test_win32_tray_set_state_clears_nif_info_flag(self):
        """Win32 tray set_state must strip NIF_INFO and clear szInfo so old balloons never replay on connect."""
        tray = object.__new__(_Win32Tray)
        tray._available = True
        tray._hwnd = 12345
        tray._current_hicon = 99
        tray._nid = mock.MagicMock()
        tray._nid.hIcon = 99
        tray._nid_lock = threading.Lock()
        tray._user32 = mock.MagicMock()
        tray._ctypes = mock.MagicMock()
        tray._shell32 = mock.MagicMock()
        tray._shell32.Shell_NotifyIconW.return_value = 1

        observed_flags = []
        observed_infos = []
        def capture_modify(msg, ptr):
            observed_flags.append(tray._nid.uFlags)
            observed_infos.append(tray._nid.szInfo)
            return 1

        tray._shell32.Shell_NotifyIconW.side_effect = capture_modify

        tray.notify("VPN Disconnected", "Connection aborted by user.", level="alarm")
        # When notify sends balloon, NIF_INFO (0x10) is set, then cleared right after
        assert observed_flags[0] == (1 | 2 | 4 | 0x10)
        assert observed_infos[0] == "Connection aborted by user."
        assert tray._nid.uFlags == (1 | 2 | 4)
        assert tray._nid.szInfo == ""
        assert tray._nid.szInfoTitle == ""

        # Subsequent set_state("connected") must keep NIF_INFO cleared and szInfo empty
        tray.set_state("connected")
        assert observed_flags[1] == (1 | 2 | 4)
        assert observed_infos[1] == ""

    def test_softether_and_ras_owner_guards_prevent_stale_cleanup(self):
        """Superseded SoftEther and Windows RAS attempts must not tear down newer session's account, profile, or routes."""
        # 1. SoftEther ownership
        se1 = SoftEtherBackend()
        se2 = SoftEtherBackend()
        oid1 = se1._claim_ownership()
        oid2 = se2._claim_ownership()
        assert oid2 > oid1

        with mock.patch.object(SoftEtherBackend, "_run_commands") as mock_se_cmd:
            se1.abort(mock.MagicMock())
            mock_se_cmd.assert_not_called()
            se1.disconnect({"interface": "eth0", "detail": {"owner_id": oid1}}, mock.MagicMock())
            mock_se_cmd.assert_not_called()

        # 2. Windows RAS ownership
        p1 = _claim_ras_profile_owner("VPNGate")
        _claim_ras_route_owner({"9.9.9.9"}, p1)
        p2 = _claim_ras_profile_owner("VPNGate")
        _claim_ras_route_owner({"9.9.9.9"}, p2)

        with mock.patch("sys.platform", "win32"), \
             mock.patch("subprocess.run") as mock_run, \
             mock.patch("vpngate.backends._remove_windows_vpn_pbk_entry") as mock_pbk:
            _cleanup_windows_ras("VPNGate", {"9.9.9.9"}, owner_id=p1)
            mock_pbk.assert_not_called()
            mock_run.assert_not_called()

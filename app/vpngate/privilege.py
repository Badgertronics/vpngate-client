"""Start the privileged worker process.

Two mechanisms are supported:

``pkexec``
    Preferred.  The desktop session's polkit agent shows a password dialog
    and the worker runs as root afterwards.

``sudo``
    Fallback for sessions without a polkit agent (for instance when the
    application is not part of a desktop login, or polkit is detached).
    The password is entered in the application and piped into
    ``sudo -S``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from typing import Optional, Tuple

from .config import BASE_DIR

WORKER_SCRIPT = os.path.join(BASE_DIR, "worker_main.py")

# pkexec gives up quickly - and without a worker ever starting - when no
# authentication agent can drive the password prompt.
_AUTH_FAILURES = (
    "textual authentication agent",
    "controlling terminal",
    "Request dismissed",
    "not authorized",
    "authentication failed",
)


class BootstrapError(RuntimeError):
    """The helper process could not be started under the chosen mechanism."""


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = BASE_DIR + os.pathsep + env.get("PYTHONPATH", "")
    return env


def worker_command() -> Tuple[str, ...]:
    for candidate_name in ("vpngate-worker", "vpngate-worker.exe"):
        for directory in (BASE_DIR, os.path.dirname(sys.executable)):
            candidate = os.path.join(directory, candidate_name)
            if os.path.isfile(candidate) and (os.access(candidate, os.X_OK) or sys.platform == "win32"):
                return (candidate,)
    if not getattr(sys, "frozen", False):
        python = shutil.which("python3") or shutil.which("python") or sys.executable
    else:
        python = shutil.which("python3") or shutil.which("python") or "/usr/bin/python3"
    return (python, WORKER_SCRIPT)


def has_pkexec() -> bool:
    return bool(shutil.which("pkexec"))


def has_sudo() -> bool:
    return bool(shutil.which("sudo"))


def is_root() -> bool:
    if hasattr(os, "geteuid"):
        return os.geteuid() == 0
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _spawn(args: Tuple[str, ...], env: dict) -> subprocess.Popen:
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1, env=env, cwd=BASE_DIR, **kwargs)


def _drained_stderr(proc: subprocess.Popen) -> str:
    try:
        if proc.stderr:
            return proc.stderr.read() or ""
    except (OSError, ValueError):
        pass
    return ""


def _via_pkexec(env: dict) -> subprocess.Popen:
    """Start through pkexec and verify it actually took off.

    pkexec either execs the worker (staying alive for the worker's whole
    lifetime) or exits immediately with an authentication problem.  Watch
    for the decision, but never block the interface for long.
    """
    proc = _spawn(("pkexec",) + worker_command(), env)
    deadline = time.time() + 2.5
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.1)
    if proc.poll() is None:
        return proc                       # running as root, decision passed
    stderr = _drained_stderr(proc)
    if "Traceback" in stderr:
        raise BootstrapError(
            "the helper process failed to start:\n" + stderr.strip()[-300:])
    reason = stderr.strip() or f"pkexec exited with code {proc.returncode}"
    for needle in _AUTH_FAILURES:
        if needle in reason:
            raise BootstrapError(
                "no polkit agent is available in this session "
                f"({reason})")
    raise BootstrapError(f"pkexec refused to start the helper: {reason}")


def _via_sudo(env: dict, password: Optional[str]) -> subprocess.Popen:
    """Start through ``sudo -S``; the first stdin line is the password.

    Wrong passwords show up later as ``sudo: ... incorrect password``
    lines on the helper's stderr, at which point the caller clears the
    remembered password and asks again.
    """
    proc = _spawn(("sudo", "-S", "-p", "") + worker_command(), env)
    if password:
        try:
            proc.stdin.write(password + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass
    return proc


def spawn(sudo_password: Optional[str] = None) -> Tuple[subprocess.Popen, str]:
    """Start the worker.  Returns (process, method).

    :raises ``BootstrapError``: when pkexec is unable to run the helper.
        Callers are expected to ask the user for a sudo password and
        retry with it, which routes directly to the sudo mechanism.
    """
    env = _env()
    if sys.platform == "win32":
        return _spawn(worker_command(), env), "windows"
    if is_root():
        return _spawn(worker_command(), env), "root"
    if not sudo_password and has_pkexec():
        proc = _via_pkexec(env)
        return proc, "pkexec"
    if not has_sudo():
        raise BootstrapError("neither pkexec nor sudo is available")
    return _via_sudo(env, sudo_password), "sudo"

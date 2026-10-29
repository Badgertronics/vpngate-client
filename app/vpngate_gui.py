#!/usr/bin/env python3
"""Launcher of the VPN Gate graphical client."""
import os
import sys

# On Windows, GTK4's OpenGL renderer fails on popups/menus without Direct Composition
# ("Failed to realize renderer 'GskGLRenderer' for surface 'GdkWin32Popup'").
# Cairo is native, extremely reliable, and avoids this requirement.
# On Linux, Mesa/RADV prints noisy Vulkan driver warnings; GL is preferred.
if sys.platform == "win32":
    os.environ.setdefault("GSK_RENDERER", "cairo")
else:
    os.environ.setdefault("GSK_RENDERER", "gl")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vpngate.ui import main  # noqa: E402

if __name__ == "__main__":
    main()

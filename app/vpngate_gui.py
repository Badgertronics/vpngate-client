#!/usr/bin/env python3
"""Launcher of the VPN Gate graphical client."""
import os
import sys

# The RADV/Mesa Vulkan loader prints noisy "not using the AMDGPU kernel
# driver" errors on some systems when GTK tries the Vulkan renderer; the GL
# renderer (renamed from 'ngl') is the sensible default for this client.
os.environ.setdefault("GSK_RENDERER", "gl")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vpngate.ui import main  # noqa: E402

if __name__ == "__main__":
    main()

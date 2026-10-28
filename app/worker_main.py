#!/usr/bin/env python3
"""Entry point of the privileged VPN Gate worker."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vpngate.worker import main  # noqa: E402

if __name__ == "__main__":
    main()

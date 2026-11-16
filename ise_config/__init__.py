"""Cisco ISE configuration as code: export to git, rebuild a fresh cluster from it."""

import os
import sys

__version__ = "0.2.0"

# Vendored pure-Python dependencies take precedence over system packages,
# so the tool runs offline on Python 3.8+ with nothing installed.
_VENDOR = os.path.join(os.path.dirname(__file__), "_vendor")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

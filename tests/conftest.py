"""Shared test setup."""

import os

# Tests must never download tools (npm opencli) from the network.
os.environ.setdefault("OPENMD_AUTO_INSTALL_TOOLS", "0")

"""Agent Upgrade Gate: CI for model and harness changes."""

import os as _os

_os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")  # mini-swe-agent prints a banner on import otherwise

__version__ = "0.1.0"

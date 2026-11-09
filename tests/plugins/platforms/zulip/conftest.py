"""Plugin-local test setup.

The ``zulip`` SDK is this plugin's ``python_dependencies`` entry, so it is NOT
synced into the core ``all``-based test environment. Install a minimal mock in
``sys.modules`` so ``import zulip`` inside the adapter/tools resolves to a
patchable ``MagicMock``. Idempotent; skipped when the real SDK is installed.
"""

import sys
from unittest.mock import MagicMock


def _ensure_zulip_mock() -> None:
    if "zulip" in sys.modules and hasattr(sys.modules["zulip"], "__file__"):
        return
    sys.modules["zulip"] = MagicMock()


# Run at collection time — before any test file's module-level imports.
_ensure_zulip_mock()

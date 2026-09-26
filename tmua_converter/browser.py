"""Locating and launching a headless Chromium through Playwright (optional)."""

from __future__ import annotations

import os
from pathlib import Path

# Pre-installed browsers some environments ship outside Playwright's cache.
_FALLBACK_EXECUTABLES = ("/opt/pw-browsers/chromium",)


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def launch_chromium(playwright):
    """Launch Chromium, honouring ``TMUA_CHROMIUM_PATH`` for a custom binary."""
    explicit = os.environ.get("TMUA_CHROMIUM_PATH")
    if explicit:
        return playwright.chromium.launch(executable_path=explicit)
    try:
        return playwright.chromium.launch()
    except Exception:
        for candidate in _FALLBACK_EXECUTABLES:
            if Path(candidate).exists():
                return playwright.chromium.launch(executable_path=candidate)
        raise

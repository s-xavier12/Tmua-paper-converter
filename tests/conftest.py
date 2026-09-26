import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent))

from make_sample_paper import OUT as SAMPLE_PDF  # noqa: E402


def _chromium_ok() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        from tmua_converter.browser import launch_chromium
        with sync_playwright() as p:
            launch_chromium(p).close()
        return True
    except Exception:  # noqa: BLE001
        return False


CHROMIUM = _chromium_ok()
needs_chromium = pytest.mark.skipif(not CHROMIUM, reason="playwright/Chromium not available")


@pytest.fixture(scope="session")
def sample_pdf() -> Path:
    if not SAMPLE_PDF.exists():
        if not CHROMIUM:
            pytest.skip("sample PDF missing and Chromium unavailable to build it")
        from make_sample_paper import build
        build()
    return SAMPLE_PDF

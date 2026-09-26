"""Headless rendering of converted questions with KaTeX (optional).

Rendering each question exactly as a simulator would lets the verifier compare
*what the student will see* against the PDF, which is where grouping mistakes
(a term inside instead of outside a fraction, a lost exponent) are most
visible.  It also reports every expression KaTeX cannot parse.

Requires the ``playwright`` package and a Chromium build; when unavailable the
pipeline falls back to comparing LaTeX source against the page image.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from .browser import launch_chromium, playwright_available

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


@dataclass
class RenderResult:
    png: bytes
    katex_errors: dict[str, str]  # field -> message


class QuestionRenderer:
    """One browser for many renders.  Not thread-safe; guarded by a lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pw = None
        self._browser = None
        self._page = None

    def __enter__(self) -> "QuestionRenderer":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        try:
            self._browser = launch_chromium(self._pw)
            self._page = self._browser.new_page(device_scale_factor=2)
            self._page.goto((STATIC_DIR / "render.html").as_uri())
            self._page.wait_for_function("typeof window.renderForCheck === 'function' && typeof katex !== 'undefined'")
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        for obj, meth in ((self._browser, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    getattr(obj, meth)()
                except Exception:  # noqa: BLE001
                    pass
        self._browser = self._pw = self._page = None

    def render(self, question: dict) -> RenderResult:
        with self._lock:
            errors = self._page.evaluate("q => window.renderForCheck(q)", question)
            png = self._page.locator("#q").screenshot(type="png")
        return RenderResult(png=png, katex_errors=dict(errors or {}))


def try_start_renderer() -> QuestionRenderer | None:
    """Start a renderer, or return None (with a log message) if unavailable."""
    if not playwright_available():
        log.info("playwright not installed - rendered-preview checks disabled")
        return None
    r = QuestionRenderer()
    try:
        r.start()
    except Exception as exc:  # noqa: BLE001
        log.warning("could not start headless Chromium (%s) - rendered-preview checks disabled", exc)
        return None
    return r

"""Browser lifecycle: one headed-capable Chromium per run, exposed over CDP so a human
(or the scripted demo operator) can attach to the *same* live session."""

from __future__ import annotations

import shutil
import socket
from pathlib import Path
from typing import Callable

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright

from cua.surface.playwright_web import DOM_JS

CAPTURE_JS = (Path(__file__).parent.parent / "handoff" / "capture.js").read_text()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BrowserSession:
    def __init__(self, run_dir: Path, *, headed: bool = False, trace: bool = True) -> None:
        self.run_dir = run_dir
        self.headed = headed
        self.trace = trace
        self.cdp_port = free_port()
        self._pw: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._human_listener: Callable[[dict, dict], None] | None = None
        self.restricted = run_dir / "restricted"  # unredactable artifacts: never committed

    @property
    def cdp_endpoint(self) -> str:
        return f"http://127.0.0.1:{self.cdp_port}"

    def on_human_event(self, fn: Callable[[dict, dict], None]) -> None:
        self._human_listener = fn

    def _binding(self, source: dict, payload: dict) -> None:
        if self._human_listener:
            frame = source.get("frame")
            self._human_listener({"frame_name": getattr(frame, "name", ""), "frame_url": getattr(frame, "url", "")}, payload)

    def start(self) -> Page:
        self.restricted.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self.context = self._pw.chromium.launch_persistent_context(
            str(self.restricted / "profile"),
            headless=not self.headed,
            args=[f"--remote-debugging-port={self.cdp_port}"],
            viewport={"width": 1280, "height": 760},
        )
        self.context.expose_binding("__cuaHuman", self._binding)
        self.context.add_init_script(DOM_JS + "\n" + CAPTURE_JS)
        if self.trace:
            self.context.tracing.start(screenshots=True, snapshots=True)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        return self.page

    def close(self) -> Path | None:
        trace_path = None
        try:
            if self.context and self.trace:
                trace_path = self.restricted / "trace.zip"
                self.context.tracing.stop(path=str(trace_path))
            if self.context:
                self.context.close()
        finally:
            if self._pw:
                self._pw.stop()
            shutil.rmtree(self.restricted / "profile", ignore_errors=True)  # cookies = session tokens
        return trace_path

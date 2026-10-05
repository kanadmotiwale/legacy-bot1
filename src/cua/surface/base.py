"""The Surface protocol: how we perceive and act on an application.

The replay engine and the recorder depend only on this protocol plus the artifact
schema. `PlaywrightWebSurface` is the one implementation; a `DesktopSurface` (UIA/AX)
or a coordinate-based `LegacyWebSurface` would implement the same methods.
"""

from __future__ import annotations

from typing import Any, Protocol

from cua.artifact.schema import Condition, FrameRef, Locator
from cua.surface.types import ActionRequest, ActionResult, Location, MatchInfo, Observation


class Surface(Protocol):
    kind: str

    def observe(self, *, screenshot: bool = True, mask_fields: bool = False) -> Observation: ...

    def act(self, action: ActionRequest) -> ActionResult: ...

    def match(self, frame_path: list[FrameRef], locator: Locator, ref: str | None = None) -> MatchInfo:
        """How many elements does this (concrete) locator match? Optionally: is the unique match `ref`?"""
        ...

    def read(self, frame_path: list[FrameRef], locator: Locator) -> str: ...

    def check(self, condition: Condition, params: dict[str, Any]) -> bool: ...

    def read_lines_after(self, anchor_text: str, frame_path: list[FrameRef] | None) -> list[str]: ...

    def reload_failed_documents(self) -> int: ...

    def screenshot(self, *, mask_fields: bool = True) -> bytes: ...

    def current_location(self) -> Location: ...

    def wait(self, ms: int) -> None:
        """Yield to the surface's event loop (used for condition polling, never as a fixed sleep for state)."""
        ...

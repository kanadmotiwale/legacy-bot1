"""Surface-neutral observation and action types. No Playwright types leak past this seam."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field

from cua.artifact.schema import FrameRef, Locator

CONTROL_ROLES = {"textbox", "combobox", "checkbox", "radio", "button", "link"}


class UIElement(BaseModel):
    """One perceivable element. On a desktop surface this would come from UIA/AX."""

    ref: str = Field(description="valid only within the observation that produced it")
    frame_path: list[FrameRef] | None
    role: str
    name: str = ""
    tag: str = ""
    value: str | None = None
    label: str | None = None
    near_label: str | None = None
    column: str | None = None
    row: dict[str, str] | None = None
    emphasis: bool = False
    href: str | None = None
    options: list[str] | None = None
    css: str = ""
    sensitive_field: str | None = Field(None, description="set by redaction field rules")


class FrameInfo(BaseModel):
    path: list[FrameRef]
    url: str
    status: int | None = None


class Observation(BaseModel):
    url: str
    frames: list[FrameInfo]
    elements: list[UIElement]
    screenshot: bytes | None = Field(None, exclude=True)

    def digest(self) -> str:
        """Stable hash used for no-progress detection (ignores refs)."""
        body = [(f.url) for f in self.frames] + [
            (e.role, e.name, e.value, e.column) for e in self.elements
        ]
        return hashlib.sha256(json.dumps(body).encode()).hexdigest()[:16]

    def by_ref(self, ref: str) -> UIElement | None:
        return next((e for e in self.elements if e.ref == ref), None)

    def outline(self, max_elements: int = 250) -> str:
        """Compact text view for the LLM: one line per element, grouped by frame."""
        lines: list[str] = []
        current = None
        for e in self.elements[:max_elements]:
            key = json.dumps([f.model_dump() for f in e.frame_path])
            if key != current:
                current = key
                path = " > ".join(f.name or f.url_pattern or "?" for f in e.frame_path) or "top"
                url = next((f.url for f in self.frames if f.path == e.frame_path), "")
                lines.append(f"## frame: {path}  ({url})")
            bits = [f"[{e.ref}]", e.role, json.dumps(e.name)]
            if e.value is not None:
                bits.append(f"value={json.dumps(e.value)}")
            if e.label:
                bits.append(f"label={json.dumps(e.label)}")
            if e.near_label and e.role in CONTROL_ROLES:
                bits.append(f"beside={json.dumps(e.near_label)}")
            if e.column:
                bits.append(f"column={json.dumps(e.column)}")
            if e.options:
                bits.append(f"options={json.dumps(e.options)}")
            if e.emphasis:
                bits.append("(emphasis)")
            lines.append(" ".join(bits))
        if len(self.elements) > max_elements:
            lines.append(f"... {len(self.elements) - max_elements} more elements omitted")
        return "\n".join(lines)


class Location(BaseModel):
    url: str
    frames: list[FrameInfo]


class ResolvedTarget(BaseModel):
    frame_path: list[FrameRef] | None
    locator: Locator


class RefTarget(BaseModel):
    ref: str


class ActionRequest(BaseModel):
    kind: Literal["click", "type", "select", "navigate"]
    target: ResolvedTarget | RefTarget | None = None
    value: str | None = None
    timeout_ms: int = 10_000


class ActionResult(BaseModel):
    ok: bool
    error: str | None = None
    duration_ms: int = 0


class MatchInfo(BaseModel):
    count: int
    same_as_ref: bool | None = None
    error: str | None = None

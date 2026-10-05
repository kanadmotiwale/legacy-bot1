"""Locator resolution: try candidates in priority order, require a unique match.

A lower-priority match is accepted only after a short grace period (so a half-loaded
page can't make us pick the CSS fallback when role+name is about to appear), and is
reported as a drift signal.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from cua.artifact.schema import Target
from cua.replay.result import LocatorAttempt
from cua.surface.types import ResolvedTarget

LOWER_PRIORITY_GRACE_MS = 800


@dataclass
class Resolution:
    target: ResolvedTarget | None
    index: int | None
    attempts: list[LocatorAttempt] = field(default_factory=list)
    interrupted: bool = False  # a detector fired while waiting

    @property
    def ambiguous(self) -> bool:
        return self.target is None and any((a.match_count or 0) > 1 for a in self.attempts)


def resolve_target(surface, target: Target, timeout_ms: int, interrupt: Callable[[], bool] | None = None) -> Resolution:
    deadline = time.monotonic() + timeout_ms / 1000
    low_seen: float | None = None
    while True:
        attempts: list[LocatorAttempt] = []
        unique: list[int] = []
        for i, cand in enumerate(target.candidates):
            m = surface.match(target.frame_path, cand)
            attempts.append(LocatorAttempt(strategy=cand.strategy, match_count=m.count, error=m.error))
            if m.count == 1:
                unique.append(i)
        now = time.monotonic()
        if unique and unique[0] == 0:
            return Resolution(ResolvedTarget(frame_path=target.frame_path, locator=target.candidates[0]), 0, attempts)
        if unique:
            low_seen = low_seen or now
            if (now - low_seen) * 1000 >= LOWER_PRIORITY_GRACE_MS or now >= deadline:
                i = unique[0]
                return Resolution(ResolvedTarget(frame_path=target.frame_path, locator=target.candidates[i]), i, attempts)
        if interrupt and interrupt():
            return Resolution(None, None, attempts, interrupted=True)
        if now >= deadline:
            return Resolution(None, None, attempts)
        surface.wait(100)

"""Control lock: who is allowed to drive the live session, as an explicit state machine.

RUNNING (automation) -> PAUSED_FOR_HUMAN (nobody) -> HUMAN_IN_CONTROL (human)
  -> RESUMING (nobody; automation may only observe) -> RUNNING | PAUSED_FOR_HUMAN | FAILED
Terminal: COMPLETED, ABORTED, FAILED.

A fencing epoch increments every time automation (re)acquires control. Automation
acts with the epoch it acquired; a stale epoch is rejected even if the state looks
right, so an action decided before a handoff can never land after it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from cua.surface.types import ActionRequest, ActionResult


class SessionState(StrEnum):
    RUNNING = "RUNNING"
    PAUSED_FOR_HUMAN = "PAUSED_FOR_HUMAN"
    HUMAN_IN_CONTROL = "HUMAN_IN_CONTROL"
    RESUMING = "RESUMING"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"
    FAILED = "FAILED"


class Owner(StrEnum):
    AUTOMATION = "automation"
    HUMAN = "human"
    NONE = "none"


S = SessionState
TRANSITIONS: dict[tuple[SessionState, str], SessionState] = {
    (S.RUNNING, "escalate"): S.PAUSED_FOR_HUMAN,
    (S.RUNNING, "complete"): S.COMPLETED,
    (S.RUNNING, "fail"): S.FAILED,
    (S.PAUSED_FOR_HUMAN, "take_control"): S.HUMAN_IN_CONTROL,
    (S.PAUSED_FOR_HUMAN, "approve"): S.RESUMING,
    (S.PAUSED_FOR_HUMAN, "abort"): S.ABORTED,
    (S.PAUSED_FOR_HUMAN, "expire"): S.ABORTED,
    (S.HUMAN_IN_CONTROL, "resume"): S.RESUMING,
    (S.HUMAN_IN_CONTROL, "abort"): S.ABORTED,
    (S.RESUMING, "verified"): S.RUNNING,
    (S.RESUMING, "mismatch"): S.PAUSED_FOR_HUMAN,
    (S.RESUMING, "fail"): S.FAILED,
}
OWNER = {
    S.RUNNING: Owner.AUTOMATION,
    S.HUMAN_IN_CONTROL: Owner.HUMAN,
}


class IllegalTransition(Exception):
    pass


class LockViolation(Exception):
    pass


class ControlLock:
    def __init__(self, run_id: str, state_file: Path | None = None) -> None:
        self.run_id = run_id
        self.state = S.RUNNING
        self.epoch = 1
        self.state_file = state_file
        self.history: list[dict] = []
        self._persist("start", "system")

    @property
    def owner(self) -> Owner:
        return OWNER.get(self.state, Owner.NONE)

    def transition(self, event: str, actor: str, reason: str = "") -> SessionState:
        nxt = TRANSITIONS.get((self.state, event))
        if nxt is None:
            raise IllegalTransition(f"{event!r} not allowed from {self.state}")
        self.state = nxt
        if nxt == S.RUNNING:
            self.epoch += 1  # automation re-acquires: new fencing token
        self._persist(event, actor, reason)
        return nxt

    def token(self) -> int:
        if self.state != S.RUNNING:
            raise LockViolation(f"automation does not hold the lock (state={self.state})")
        return self.epoch

    def assert_automation(self, token: int) -> None:
        if self.state != S.RUNNING:
            raise LockViolation(f"automation may not act: state={self.state}, owner={self.owner}")
        if token != self.epoch:
            raise LockViolation(f"stale fencing token {token} (current epoch {self.epoch})")

    def _persist(self, event: str, actor: str, reason: str = "") -> None:
        self.history.append(
            {
                "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "event": event,
                "actor": actor,
                "state": self.state.value,
                "owner": self.owner.value,
                "epoch": self.epoch,
                "reason": reason,
            }
        )
        if self.state_file:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {"run_id": self.run_id, "state": self.state, "owner": self.owner, "epoch": self.epoch, "history": self.history},
                    indent=2,
                )
            )
            tmp.replace(self.state_file)


class GuardedSurface:
    """Wraps a Surface so that no code path can act without holding the lock."""

    def __init__(self, inner, lock: ControlLock) -> None:
        self._inner = inner
        self._lock = lock
        self._token = lock.token()

    def refresh_token(self) -> None:
        self._token = self._lock.token()

    def act(self, action: ActionRequest) -> ActionResult:
        self._lock.assert_automation(self._token)
        return self._inner.act(action)

    def __getattr__(self, name):  # observation/reads are delegated unguarded
        return getattr(self._inner, name)

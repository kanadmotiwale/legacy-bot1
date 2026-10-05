"""Pause automation, hand the live session to a human, wait for a decision, hand back.

Shared by discovery and replay. It never decides what to do after resume - the caller
re-observes and re-verifies (never assume the human left the app where we expect).
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from cua.evidence.log import RunLog, capture
from cua.handoff.interventions import InterventionRequest, InterventionStore, now
from cua.handoff.lock import ControlLock, IllegalTransition, SessionState


@dataclass
class HandoffOutcome:
    decision: Literal["approved", "resumed", "aborted", "timeout", "detached"]
    intervention_id: str
    human_actions: list[dict[str, Any]] = field(default_factory=list)
    operator: str | None = None


DECISION_EVENTS = {"take_control": "take_control", "approve": "approve", "resume": "resume", "abort": "abort"}


class HandoffCoordinator:
    def __init__(
        self,
        *,
        run_dir: Path,
        lock: ControlLock,
        log: RunLog,
        surface,
        mode: Literal["discovery", "replay"],
        cdp_endpoint: str | None,
        timeout_s: float,
        bring_to_front=None,
        poll_ms: int = 250,
    ) -> None:
        self.run_dir = run_dir
        self.lock = lock
        self.log = log
        self.surface = surface
        self.mode = mode
        self.cdp_endpoint = cdp_endpoint
        self.timeout_s = timeout_s
        self.bring_to_front = bring_to_front
        self.poll_ms = poll_ms
        self.store = InterventionStore(run_dir)
        self._human: list[dict] = []
        self.human_raw: list[tuple[dict, dict]] = []  # unredacted, in memory only (recorder input)

    # Called by the browser binding for every DOM click/change in any frame.
    def on_human_event(self, source: dict, payload: dict) -> None:
        if self.lock.state != SessionState.HUMAN_IN_CONTROL:
            return  # automation's own clicks also fire DOM events; only record the human's
        rec = {
            "kind": payload.get("kind"),
            "role": payload.get("role"),
            "name": payload.get("name"),
            "label": payload.get("label") or payload.get("near_label"),
            "column": payload.get("column"),
            "value": payload.get("value"),
            "frame": source.get("frame_name") or source.get("frame_url"),
            "css": payload.get("css"),
        }
        self.human_raw.append((source, payload))
        rec = self.log.redactor.obj(rec)
        self._human.append(rec)
        self.log.event("human_action", **rec)

    def escalate(
        self,
        *,
        kind: Literal["approval", "stuck", "unrecoverable"],
        reason: str,
        capability_id: str | None = None,
        goal: str | None = None,
        step_id: str | None = None,
        step_index: int | None = None,
        step_description: str | None = None,
        proposed_action: dict | None = None,
        expected_after_resume: str | None = None,
    ) -> HandoffOutcome:
        iid = "iv-" + secrets.token_hex(4)
        self.lock.transition("escalate", "automation", reason)
        ev = capture(self.surface, self.log, f"handoff-{iid}")
        allowed = ["approve", "take_control", "abort"] if kind == "approval" else ["take_control", "abort"]
        req = InterventionRequest(
            id=iid,
            run_id=self.log.run_id,
            kind=kind,
            created_at=now(),
            mode=self.mode,
            capability_id=capability_id,
            goal=self.log.redactor.text(goal) if goal else None,
            step_id=step_id,
            step_index=step_index,
            step_description=step_description,
            reason=self.log.redactor.text(reason),
            current_url=self.log.redactor.text(self._where()),
            screenshot=ev.get("screenshot"),
            a11y_snapshot=ev.get("a11y_snapshot"),
            recent_events=self.log.recent[-10:],
            proposed_action=self.log.redactor.obj(proposed_action) if proposed_action else None,
            expected_after_resume=expected_after_resume,
            cdp_endpoint=self.cdp_endpoint,
            allowed_decisions=allowed,
        )
        self.store.save(req)
        self.log.event("intervention_requested", intervention_id=iid, kind=kind, reason=reason, step_id=step_id, evidence=ev)

        if self.timeout_s <= 0:
            self.lock.transition("expire", "system", "no operator attached (unattended run)")
            req.status, req.resolution = "expired", "detached: returned NeedsHuman to caller"
            self.store.save(req)
            return HandoffOutcome("detached", iid)

        self._human = []
        consumed = 0
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            decisions = self.store.decisions(iid)
            for d in decisions[consumed:]:
                consumed += 1
                outcome = self._apply(req, d)
                if outcome:
                    return outcome
            self.surface.wait(self.poll_ms)  # pumps the browser event loop so human events arrive
        self.lock.transition("expire" if self.lock.state == SessionState.PAUSED_FOR_HUMAN else "abort", "system", "operator timeout")
        req.status, req.resolution = "expired", "operator did not respond in time"
        self.store.save(req)
        self.log.event("intervention_timeout", intervention_id=iid)
        return HandoffOutcome("timeout", iid, list(self._human))

    def _where(self) -> str:
        """Every frame's URL: in a frameset app the top URL alone says nothing."""
        loc = self.surface.current_location()
        parts = [f"{'/'.join(f.name or f.url_pattern or '?' for f in fr.path) or 'top'}={fr.url}" for fr in loc.frames]
        return "; ".join(parts) or loc.url

    def _apply(self, req: InterventionRequest, d: dict) -> HandoffOutcome | None:
        decision, operator = d.get("decision"), d.get("operator", "operator")
        if decision not in req.allowed_decisions + ["resume"]:
            self.log.event("decision_rejected", intervention_id=req.id, decision=decision, why="not offered")
            return None
        try:
            self.lock.transition(DECISION_EVENTS[decision], f"human:{operator}", d.get("note", ""))
        except IllegalTransition as e:
            self.log.event("decision_rejected", intervention_id=req.id, decision=decision, why=str(e))
            return None
        self.log.event("operator_decision", intervention_id=req.id, decision=decision, operator=operator, state=self.lock.state)
        if decision == "take_control":
            req.status = "human_in_control"
            self.store.save(req)
            if self.bring_to_front:
                self.bring_to_front()
            return None
        req.human_actions = list(self._human)
        if decision == "abort":
            req.status, req.resolution = "aborted", f"aborted by {operator}"
            self.store.save(req)
            return HandoffOutcome("aborted", req.id, list(self._human), operator)
        req.status, req.resolution = "resolved", f"{decision} by {operator}"
        self.store.save(req)
        return HandoffOutcome("approved" if decision == "approve" else "resumed", req.id, list(self._human), operator)

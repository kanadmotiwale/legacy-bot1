"""Intervention requests: persisted, one writer per file.

runs/<run_id>/interventions/<iid>/request.json   written only by the runner
runs/<run_id>/interventions/<iid>/decisions.jsonl appended only by operators
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

Decision = Literal["take_control", "approve", "resume", "abort"]


class InterventionRequest(BaseModel):
    id: str
    run_id: str
    kind: Literal["approval", "stuck", "unrecoverable"]
    created_at: str
    mode: Literal["discovery", "replay"]
    capability_id: str | None = None
    goal: str | None = None
    step_id: str | None = None
    step_index: int | None = None
    step_description: str | None = None
    reason: str
    current_url: str | None = None
    screenshot: str | None = None
    a11y_snapshot: str | None = None
    recent_events: list[dict[str, Any]] = Field(default_factory=list)
    proposed_action: dict[str, Any] | None = None
    expected_after_resume: str | None = None
    cdp_endpoint: str | None = None
    allowed_decisions: list[Decision]
    status: Literal["open", "human_in_control", "resolved", "aborted", "expired"] = "open"
    resolution: str | None = None
    human_actions: list[dict[str, Any]] = Field(default_factory=list)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class InterventionStore:
    def __init__(self, run_dir: Path) -> None:
        self.root = run_dir / "interventions"

    def dir(self, iid: str) -> Path:
        return self.root / iid

    def save(self, req: InterventionRequest) -> Path:
        d = self.dir(req.id)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "request.tmp"
        tmp.write_text(req.model_dump_json(indent=2))
        tmp.replace(d / "request.json")
        return d / "request.json"

    def decisions(self, iid: str) -> list[dict]:
        p = self.dir(iid) / "decisions.jsonl"
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def submit_decision(request_dir: Path, decision: Decision, operator: str, note: str = "") -> None:
    """Operator side (UI, CLI or scripted operator)."""
    with (request_dir / "decisions.jsonl").open("a") as f:
        f.write(json.dumps({"decision": decision, "operator": operator, "note": note, "at": now()}) + "\n")


def list_requests(runs_root: Path) -> list[tuple[Path, InterventionRequest]]:
    out = []
    for p in sorted(runs_root.glob("*/interventions/*/request.json")):
        try:
            out.append((p.parent, InterventionRequest.model_validate_json(p.read_text())))
        except ValueError:
            continue
    return sorted(out, key=lambda x: x[1].created_at, reverse=True)

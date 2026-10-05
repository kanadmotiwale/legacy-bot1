"""Structured, redacted run evidence: one JSONL log per run plus snapshots on demand."""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cua.policy.redaction import Redactor


def new_run_id(kind: str) -> str:
    return f"{kind}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"


class RunLog:
    """Append-only JSONL. Every record passes through the redactor before it is written."""

    def __init__(self, run_dir: Path, run_id: str, redactor: Redactor) -> None:
        self.run_dir = run_dir
        self.run_id = run_id
        self.redactor = redactor
        self.path = run_dir / "run.jsonl"
        self._seq = 0
        self._t0 = time.monotonic()
        self.recent: list[dict] = []
        run_dir.mkdir(parents=True, exist_ok=True)

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def event(self, type_: str, **fields: Any) -> dict:
        self._seq += 1
        rec = {
            "run_id": self.run_id,
            "seq": self._seq,
            "t_ms": self.elapsed_ms,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "type": type_,
            **fields,
        }
        rec = self.redactor.obj(rec)
        with self.path.open("a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        self.recent = (self.recent + [rec])[-25:]
        return rec

    def finalize(self) -> None:
        """Re-apply everything the redactor learned during the run to all text evidence.

        Some values become known-sensitive only late (inputs are classified at `finish`;
        sensitive fields are learned when first seen), so earlier lines are re-scrubbed."""
        for p in self.run_dir.rglob("*"):
            if p.is_file() and p.suffix in (".jsonl", ".json", ".txt") and "restricted" not in p.parts:
                text = p.read_text()
                scrubbed = self.redactor.text(text)
                if scrubbed != text:
                    p.write_text(scrubbed)

    def write_json(self, name: str, data: Any) -> Path:
        p = self.run_dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.redactor.obj(data), indent=2, default=str))
        return p


def capture(surface, log: RunLog, label: str) -> dict[str, str]:
    """Screenshot (sensitive fields masked) + redacted accessibility outline."""
    out: dict[str, str] = {}
    snap_dir = log.run_dir / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    try:
        png = surface.screenshot(mask_level="log")
        p = snap_dir / f"{log._seq:03d}-{label}.png"
        p.write_bytes(png)
        out["screenshot"] = str(p.relative_to(log.run_dir))
    except Exception as e:  # evidence capture must never mask the real error
        out["screenshot_error"] = str(e).splitlines()[0]
    try:
        obs = surface.observe(screenshot=False)
        red = log.redactor.observation(obs, level="log")
        p = snap_dir / f"{log._seq:03d}-{label}.a11y.txt"
        p.write_text(log.redactor.text(red.outline(max_elements=400)))
        out["a11y_snapshot"] = str(p.relative_to(log.run_dir))
    except Exception as e:
        out["a11y_error"] = str(e).splitlines()[0]
    return out

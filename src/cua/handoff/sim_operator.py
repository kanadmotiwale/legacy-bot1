"""Scripted operator for the non-interactive demo.

Runs as a SEPARATE process, attaches to the run's live browser over CDP (the same
session, not a fresh one), performs a few manual actions, and hands control back -
exactly what a human would do through the operator UI + browser window.

    python -m cua.handoff.sim_operator --runs-dir runs --mode take_control \
        --click "button:Acknowledge"
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import typer

from cua.handoff.interventions import InterventionRequest, submit_decision

app = typer.Typer(add_completion=False)


def _wait_for_request(runs_dir: Path, run_id: str | None, timeout: float) -> tuple[Path, InterventionRequest]:
    deadline = time.monotonic() + timeout
    pattern = f"{run_id or '*'}/interventions/*/request.json"
    while time.monotonic() < deadline:
        for p in sorted(runs_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True):
            req = InterventionRequest.model_validate_json(p.read_text())
            if req.status == "open":
                return p.parent, req
        time.sleep(0.2)
    raise SystemExit("no open intervention request appeared")


def _wait_state(run_dir: Path, state: str, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if json.loads((run_dir / "session.json").read_text())["state"] == state:
                return
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(0.1)
    raise SystemExit(f"session never reached {state}")


@app.command()
def main(
    runs_dir: Path = typer.Option(Path("runs")),
    run_id: str | None = typer.Option(None),
    mode: str = typer.Option("take_control", help="approve | take_control | abort"),
    click: list[str] = typer.Option([], help="role:name to click while in control, e.g. 'button:Acknowledge'"),
    operator: str = typer.Option("demo-operator"),
    timeout: float = typer.Option(60),
) -> None:
    req_dir, req = _wait_for_request(runs_dir, run_id, timeout)
    run_dir = req_dir.parent.parent
    print(f"[operator] picked up {req.id} ({req.kind}): {req.reason}")
    if mode in ("approve", "abort"):
        submit_decision(req_dir, mode, operator, note="scripted demo decision")
        print(f"[operator] {mode}")
        return

    submit_decision(req_dir, "take_control", operator, note="taking over the live session")
    _wait_state(run_dir, "HUMAN_IN_CONTROL")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(req.cdp_endpoint)
        page = next(pg for ctx in browser.contexts for pg in ctx.pages if pg.url.startswith("http"))
        print(f"[operator] attached over CDP to live page {page.url}")
        for spec in click:
            role, name = spec.split(":", 1)
            for frame in page.frames:
                loc = frame.get_by_role(role, name=name, exact=True)
                if loc.count() == 1:
                    page.wait_for_timeout(400)  # human-ish pacing; also lets the runner see the event
                    loc.click()
                    print(f"[operator] clicked {role} '{name}' in frame '{frame.name or 'top'}'")
                    break
            else:
                print(f"[operator] could not find {spec}")
        page.wait_for_timeout(500)
        # no browser.close(): leaving the playwright context just drops this CDP client
    submit_decision(req_dir, "resume", operator, note="done, handing control back")
    print("[operator] resume")


if __name__ == "__main__":
    app()

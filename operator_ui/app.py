"""Minimal operator console. Deliberately crude: it lists intervention requests with their
context and lets an operator Take control / Approve / Resume / Abort. The live session itself
is the run's headed browser window (or any CDP client attached to the endpoint shown)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from cua.handoff.interventions import InterventionRequest, list_requests, submit_decision

TEMPLATES = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=select_autoescape(["html"]))
app = FastAPI(title="cua operator", docs_url=None, redoc_url=None)


def roots() -> list[Path]:
    return [Path(p) for p in os.environ.get("CUA_RUNS_DIRS", "runs").split(",") if p]


def session_state(run_dir: Path) -> dict:
    try:
        return json.loads((run_dir / "session.json").read_text())
    except (OSError, ValueError):
        return {}


def find(run_id: str, iid: str) -> tuple[Path, InterventionRequest]:
    for root in roots():
        d = root / run_id / "interventions" / iid
        if (d / "request.json").exists():
            return d, InterventionRequest.model_validate_json((d / "request.json").read_text())
    raise HTTPException(404, "intervention not found")


@app.get("/", response_class=HTMLResponse)
def index():
    items = []
    for root in roots():
        for d, req in list_requests(root):
            items.append((req, session_state(d.parent.parent)))
    items.sort(key=lambda x: x[0].created_at, reverse=True)
    return TEMPLATES.get_template("index.html").render(items=items)


@app.get("/iv/{run_id}/{iid}", response_class=HTMLResponse)
def detail(run_id: str, iid: str):
    d, req = find(run_id, iid)
    decisions = [json.loads(line) for line in (d / "decisions.jsonl").read_text().splitlines()] if (d / "decisions.jsonl").exists() else []
    state = session_state(d.parent.parent)
    return TEMPLATES.get_template("detail.html").render(req=req, state=state, decisions=decisions)


@app.post("/iv/{run_id}/{iid}/decide")
def decide(run_id: str, iid: str, decision: str = Form(...), operator: str = Form("operator"), note: str = Form("")):
    if decision not in ("take_control", "approve", "resume", "abort"):
        raise HTTPException(400, "bad decision")
    d, _ = find(run_id, iid)
    submit_decision(d, decision, operator or "operator", note)  # the runner validates it against the lock
    return RedirectResponse(f"/iv/{run_id}/{iid}", status_code=303)


@app.get("/file/{run_id}/{rel:path}")
def file(run_id: str, rel: str):
    if ".." in rel or not rel.startswith("snapshots/"):
        raise HTTPException(403, "only redacted snapshots are served")
    for root in roots():
        p = root / run_id / rel
        if p.exists():
            return FileResponse(p)
    raise HTTPException(404)

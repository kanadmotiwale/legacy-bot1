"""End-to-end against the live mock app with a real browser (no API key needed).

discovery (MockLLM) -> artifact -> replay under every injected condition -> handoffs ->
tenant reuse -> grep every file written for planted secrets / PII.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

from cua.agent.loop import DiscoveryOptions, DiscoveryRunner
from cua.agent.mock_llm import MockLLM
from cua.artifact import store
from cua.artifact.schema import Status, TenantOverride
from cua.handoff.interventions import list_requests, submit_decision
from cua.replay.engine import ReplayEngine, ReplayOptions
from tests.conftest import BASE, ROOT, inject

pytestmark = pytest.mark.browser

LOOKUP_GOAL = "Look up member 10042 and read their current savings balance"
OPEN_GOAL = "Open a new Sub-Savings sub-account nicknamed 'Rainy Day' for member 10042 with an initial deposit of 25.00 from S01"
WRITE_PARAMS = {"member_id": "10042", "account_type": "Sub-Savings", "initial_deposit": "25.00", "funding_account": "S01"}

PLANTED = ["Plant3d-S3cret!", "900-55-1234", "900-66-9876", "03/14/1984", "(555) 010-4477", "Dana Q. Whitfield",
           "12 Synthetic Ln", "12,345.67", "12345.67", "845.10", "10042", "10077"]


@pytest.fixture(scope="session")
def work(tmp_path_factory):
    return tmp_path_factory.mktemp("cua")


def auto_operator(runs: Path, decision: str, stop: threading.Event) -> None:
    """In-process stand-in for an operator clicking Approve in the UI."""
    handled = set()
    while not stop.is_set():
        for d, req in list_requests(runs):
            if req.status == "open" and req.id not in handled:
                handled.add(req.id)
                submit_decision(d, decision, "pytest-operator")
        time.sleep(0.1)


@pytest.fixture(scope="session")
def lookup_artifact(app_server, policy, profile, work):
    r = DiscoveryRunner(MockLLM(), policy, profile, DiscoveryOptions(
        runs_root=work / "runs", artifacts_root=work / "artifacts", escalation="detach", trace=False)).run(LOOKUP_GOAL, f"{BASE}/app")
    assert r.stop_reason == "goal_met", r.detail
    return r.artifact_path


@pytest.fixture(scope="session")
def open_artifact(app_server, policy, profile, work):
    stop = threading.Event()
    threading.Thread(target=auto_operator, args=(work / "runs", "approve", stop), daemon=True).start()
    try:
        r = DiscoveryRunner(MockLLM(), policy, profile, DiscoveryOptions(
            runs_root=work / "runs", artifacts_root=work / "artifacts", escalation="wait", handoff_timeout_s=30,
            trace=False)).run(OPEN_GOAL, f"{BASE}/app")
    finally:
        stop.set()
    assert r.stop_reason == "goal_met", r.detail
    return r.artifact_path


def replay(path, params, policy, work, **opt):
    opt.setdefault("escalation", "detach")
    return ReplayEngine(store.load(path), policy, ReplayOptions(base_url=BASE, runs_root=work / "runs", trace=False, **opt)).run(params)


def test_discovered_artifact_is_parameterized(lookup_artifact):
    cap = store.load(lookup_artifact)
    text = cap.model_dump_json()
    assert cap.status == Status.DRAFT and [i.name for i in cap.inputs] == ["member_id"]
    assert "10042" not in text and "{member_id}" in text and "/member/:member_id" in text
    assert {o.name for o in cap.outcomes} >= {"member_not_found", "permission_denied"}
    view = next(s for s in cap.steps if s.id == "click_view")
    assert view.target.candidates[0].strategy == "table_cell"  # row-scoped link anchored by its row


@pytest.mark.parametrize("preset,member,status,detail", [
    (None, "10077", "success", None),
    (None, "99999", "business_outcome", "member_not_found"),
    ("not_found", "10042", "business_outcome", "member_not_found"),
    ("permission", "10042", "business_outcome", "permission_denied"),
    (None, "10099", "business_outcome", "permission_denied"),
    ("interstitial", "10042", "success", "dismissed"),
    ("session_expiry", "10042", "success", "reauthenticated"),
    ("slow", "10077", "success", None),
    ("500", "10042", "success", "retry"),
    ("500_persistent", "10042", "failure", "app_error"),
    ("unknown_modal", "10042", "needs_human", None),
    (None, "abc", "failure", "invalid_input"),
])
def test_error_taxonomy(lookup_artifact, policy, work, preset, member, status, detail):
    inject(preset)
    try:
        res = replay(lookup_artifact, {"member_id": member}, policy, work)
    finally:
        inject(None)
    assert res.status == status, res.model_dump_json(indent=1)
    kinds = {e.kind for e in res.events}
    if status == "business_outcome":
        assert res.outcome == detail
    elif status == "failure":
        assert res.error_code == detail and res.expected
        if detail != "invalid_input":
            assert res.observed and res.evidence.get("screenshot")
    elif status == "success":
        assert "savings_balance" in res.outputs
        if detail:
            assert detail in kinds
    elif status == "needs_human":
        assert res.intervention_id and (Path(res.evidence_dir) / "interventions" / res.intervention_id / "request.json").exists()


def test_takeover_same_session_and_resume(lookup_artifact, policy, work):
    """A separate process attaches over CDP to the run's live browser, acts, and resumes."""
    op = subprocess.Popen([sys.executable, "-m", "cua.handoff.sim_operator", "--runs-dir", str(work / "runs"),
                           "--mode", "take_control", "--click", "button:Acknowledge", "--timeout", "60"],
                          cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    inject("unknown_modal")
    try:
        res = replay(lookup_artifact, {"member_id": "10042"}, policy, work, escalation="wait", handoff_timeout_s=60)
    finally:
        inject(None)
        out = op.communicate(timeout=30)[0]
    assert res.status == "success", res.model_dump_json(indent=1) + out
    kinds = [e.kind for e in res.events]
    assert kinds.index("intervention_requested") < kinds.index("human_action") < kinds.index("resume_verified")
    human = next(e for e in res.events if e.kind == "human_action")
    assert human.detail["name"] == "Acknowledge"


def test_write_flow_gates_and_idempotency(open_artifact, policy, work):
    cap = store.load(open_artifact)
    submit = next(s for s in cap.steps if s.risk == "irreversible")
    assert submit.id == "click_submit" and cap.idempotency is not None
    # already done during discovery -> idempotency probe short-circuits, nothing re-submitted
    assert replay(open_artifact, {**WRITE_PARAMS, "nickname": "Rainy Day"}, policy, work).already_existed
    # new effect with a draft artifact: blocked at the irreversible step
    res = replay(open_artifact, {**WRITE_PARAMS, "nickname": "Boat"}, policy, work, allow_irreversible=True)
    assert res.status == "failure" and res.error_code == "policy_blocked" and res.step_id == "click_submit"
    store.approve(open_artifact, "pytest-reviewer")
    res = replay(open_artifact, {**WRITE_PARAMS, "nickname": "Boat"}, policy, work)
    assert res.status == "needs_human" and res.step_id == "click_submit"  # approved but no explicit flag
    res = replay(open_artifact, {**WRITE_PARAMS, "nickname": "Boat"}, policy, work, allow_irreversible=True)
    assert res.status == "success" and res.outputs["reference_number"].startswith("SA-")
    again = replay(open_artifact, {**WRITE_PARAMS, "nickname": "Boat"}, policy, work, allow_irreversible=True)
    assert again.status == "success" and again.already_existed
    bad = replay(open_artifact, {**WRITE_PARAMS, "initial_deposit": "2.00", "nickname": "Tiny"}, policy, work, allow_irreversible=True)
    assert bad.status == "business_outcome" and bad.outcome == "validation_error"
    assert bad.details["field_errors"] == {"Initial Deposit": "Must be at least $5.00."}


def test_cross_tenant_override_and_drift(lookup_artifact, policy, work):
    cap = store.load(lookup_artifact)
    cap.app.tenant_overrides["lakeshore"] = TenantOverride.model_validate(yaml.safe_load((ROOT / "config/tenants/lakeshore.yaml").read_text()))
    cap.app.tenant_overrides["unmapped"] = TenantOverride(route_prefix="/tb")
    cap.version = "1.1.0"
    path = store.save(work / "artifacts", cap)
    ok = replay(path, {"member_id": "10042"}, policy, work, tenant="lakeshore")
    assert ok.status == "success" and not ok.drift
    drifted = replay(path, {"member_id": "10042"}, policy, work, tenant="unmapped", escalation="fail")
    assert drifted.status == "failure" and drifted.drift
    assert drifted.drift[0].step_id == "type_member_id" and drifted.drift[0].matched_strategy == "css"


def test_no_planted_secrets_or_pii_written(work, lookup_artifact, open_artifact):
    """Grep every file written by the runs above (except restricted traces) for planted values."""
    leaks = []
    for p in work.rglob("*"):
        if not p.is_file() or "restricted" in p.parts or p.suffix == ".png":
            continue
        text = p.read_text(errors="ignore")
        leaks += [f"{p.relative_to(work)}: {v}" for v in PLANTED if v in text]
    assert not leaks, "\n".join(leaks)

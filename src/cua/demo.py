"""`cua demo`: the whole story in one command, offline (MockLLM + scripted operator).

Writes run evidence to evidence/runs/, copies the artifacts to evidence/artifacts/ and
summarizes every scenario in evidence/SUMMARY.md.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

from cua.agent.loop import DiscoveryOptions, DiscoveryRunner
from cua.agent.mock_llm import MockLLM
from cua.artifact import store
from cua.artifact.profile import AppProfile
from cua.artifact.schema import TenantOverride
from cua.policy.gate import PolicyConfig
from cua.replay.engine import ReplayEngine, ReplayOptions

LOOKUP_GOAL = "Look up member 10042 and read their current savings balance"
OPEN_GOAL = ("Open a new Sub-Savings sub-account nicknamed 'Rainy Day' for member 10042 with an initial "
             "deposit of 25.00 from S01, and reach the confirmation screen")
WRITE = {"member_id": "10042", "account_type": "Sub-Savings", "initial_deposit": "25.00", "funding_account": "S01"}


@dataclass
class Scenario:
    sid: str
    title: str
    command: str
    expected: str
    status: str = ""
    run_dir: str = ""
    summary: str = ""

    @property
    def ok(self) -> bool:
        return self.status == self.expected


def _serve(base_url: str) -> None:
    import uvicorn

    from target_app.app import app

    port = urlsplit(base_url).port or 80
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()


def _ensure_app(base_url: str, start_app: bool) -> None:
    try:
        httpx.get(f"{base_url}/login", timeout=1)
        return
    except httpx.HTTPError:
        if not start_app:
            raise SystemExit(f"mock app not reachable at {base_url}; run `cua target-app`")
    _serve(base_url)
    for _ in range(100):
        try:
            httpx.get(f"{base_url}/login", timeout=1)
            return
        except httpx.HTTPError:
            time.sleep(0.1)
    raise SystemExit("could not start the mock app")


def _inject(base_url: str, preset: str | None) -> None:
    httpx.post(f"{base_url}/__admin/conditions", json={"preset": preset} if preset else {})


def _operator(runs: Path, mode: str, *clicks: str) -> subprocess.Popen:
    cmd = [sys.executable, "-m", "cua.handoff.sim_operator", "--runs-dir", str(runs), "--mode", mode, "--timeout", "90"]
    for c in clicks:
        cmd += ["--click", c]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def _brief(result) -> str:
    d = json.loads((Path(result.evidence_dir) / "result.json").read_text())  # the redacted, persisted copy
    keep = ["outputs", "already_existed", "outcome", "details", "error_code", "message", "step_id", "expected",
            "observed", "intervention_id", "reason", "drift"]
    return json.dumps({k: d[k] for k in keep if d.get(k) not in (None, [], {}, False, "")})


def run_demo(evidence_dir: Path, base_url: str, *, start_app: bool = True, keep_existing: bool = False) -> int:
    root = Path.cwd()
    from target_app.app import credentials

    user, pwd = credentials()
    os.environ.setdefault("MOCKBANK_USER", user)  # the demo harness plays the secret store
    os.environ.setdefault("MOCKBANK_PASSWORD", pwd)
    _ensure_app(base_url, start_app)
    httpx.post(f"{base_url}/__admin/reset")

    runs = evidence_dir / "runs"
    artifacts = root / "artifacts"
    if not keep_existing:
        shutil.rmtree(runs, ignore_errors=True)
        shutil.rmtree(evidence_dir / "artifacts", ignore_errors=True)
        for d in artifacts.glob("mockcore.*"):
            shutil.rmtree(d)
    runs.mkdir(parents=True, exist_ok=True)
    policy = PolicyConfig.load(root / "config/policy.yaml")
    origin = f"{urlsplit(base_url).scheme}://{urlsplit(base_url).netloc}"
    if origin not in policy.allowed_origins:
        policy.allowed_origins.append(origin)
    profile = AppProfile.load(root / "config/apps/mockcore.yaml")
    store.export_schema(root / "schemas/capability.schema.json")
    scenarios: list[Scenario] = []

    def record(sc: Scenario, status: str, run_dir: Path, summary: str) -> None:
        sc.status, sc.run_dir, sc.summary = status, str(run_dir.relative_to(evidence_dir)), summary
        scenarios.append(sc)
        mark = "PASS" if sc.ok else "FAIL"
        print(f"[{mark}] {sc.sid:<24} {status:<17} {sc.title}")

    def discover(sc: Scenario, goal: str, escalation: str = "detach") -> Path | None:
        r = DiscoveryRunner(MockLLM(), policy, profile, DiscoveryOptions(
            runs_root=runs, artifacts_root=artifacts, escalation=escalation, handoff_timeout_s=60)).run(goal, f"{base_url}/app")
        record(sc, r.stop_reason, r.run_dir, f"artifact {r.capability_id} v{r.version}" if r.artifact_path else r.detail)
        return r.artifact_path

    def replay(sc: Scenario, path: Path, params: dict, preset: str | None = None, **opt):
        opt.setdefault("escalation", "detach")
        _inject(base_url, preset)
        try:
            res = ReplayEngine(store.load(path), policy, ReplayOptions(base_url=base_url, runs_root=runs, **opt)).run(params)
        finally:
            _inject(base_url, None)
        record(sc, res.status, Path(res.evidence_dir), _brief(res))
        return res

    print("== discovery: member lookup (MockLLM)")
    lookup = discover(Scenario("discover-lookup", "LLM discovery of 'read savings balance' -> draft artifact",
                               f'cua discover --mock-llm --goal "{LOOKUP_GOAL}"', "goal_met"), LOOKUP_GOAL)
    if lookup is None:
        print("discovery failed; aborting demo")
        return 1
    rel = lookup.relative_to(root)

    print("== deterministic replay + error taxonomy")
    replay(Scenario("replay-success", "Different member, no LLM", f"cua replay {rel} -p member_id=10077", "success"),
           lookup, {"member_id": "10077"})
    replay(Scenario("replay-not-found", "Business outcome: no such member", f"cua replay {rel} -p member_id=99999", "business_outcome"),
           lookup, {"member_id": "99999"})
    replay(Scenario("replay-permission", "Business outcome: restricted member", f"cua replay {rel} -p member_id=10099", "business_outcome"),
           lookup, {"member_id": "10099"})
    replay(Scenario("replay-interstitial", "Recoverable: maintenance notice dismissed",
                    f"cua replay {rel} -p member_id=10042 --inject interstitial", "success"),
           lookup, {"member_id": "10042"}, "interstitial")
    replay(Scenario("replay-session-expiry", "Recoverable: session expired -> re-auth from credential ref -> restart",
                    f"cua replay {rel} -p member_id=10042 --inject session_expiry", "success"),
           lookup, {"member_id": "10042"}, "session_expiry")
    replay(Scenario("replay-transient-500", "Recoverable: one 500 -> retry with backoff",
                    f"cua replay {rel} -p member_id=10042 --inject 500", "success"),
           lookup, {"member_id": "10042"}, "500")
    replay(Scenario("replay-hard-failure", "Hard failure: 500 persists after bounded retries",
                    f"cua replay {rel} -p member_id=10042 --inject 500_persistent", "failure"),
           lookup, {"member_id": "10042"}, "500_persistent")
    replay(Scenario("replay-invalid-input", "Rejected before touching the UI", f"cua replay {rel} -p member_id=abc", "failure"),
           lookup, {"member_id": "abc"})
    replay(Scenario("replay-unknown-detached", "Unknown state, unattended -> NeedsHuman with intervention request",
                    f"cua replay {rel} -p member_id=10042 --inject unknown_modal", "needs_human"),
           lookup, {"member_id": "10042"}, "unknown_modal")

    print("== escalation: human takes over the live session (separate process over CDP), then resumes")
    op = _operator(runs, "take_control", "button:Acknowledge")
    replay(Scenario("handoff-takeover", "Stuck replay -> operator takes control of the SAME browser -> resume -> re-verify",
                    f"cua replay {rel} -p member_id=10042 --inject unknown_modal --escalation wait  (+ operator UI / `cua decide`)",
                    "success"),
           lookup, {"member_id": "10042"}, "unknown_modal", escalation="wait", handoff_timeout_s=60)
    print("   " + op.communicate(timeout=30)[0].strip().replace("\n", "\n   "))

    print("== write flow: discovery with operator approval of the irreversible submit")
    op = _operator(runs, "approve")
    opened = discover(Scenario("discover-open-subaccount", "Discovery pauses at irreversible Submit; operator approves",
                               f'cua discover --mock-llm --goal "{OPEN_GOAL}"', "goal_met"), OPEN_GOAL, escalation="wait")
    print("   " + op.communicate(timeout=30)[0].strip().replace("\n", "\n   "))
    if opened:
        orel = opened.relative_to(root)
        p = "-p member_id=10042 -p account_type=Sub-Savings -p initial_deposit=25.00 -p funding_account=S01"
        replay(Scenario("write-draft-blocked", "Draft artifact may not perform the irreversible step",
                        f'cua replay {orel} {p} -p nickname="Vacation Fund" --allow-irreversible', "failure"),
               opened, {**WRITE, "nickname": "Vacation Fund"}, allow_irreversible=True)
        store.approve(opened, "demo-reviewer", "verified submit classification and idempotency probe")
        print(f"   approved {orel} (cua approve {orel} --reviewer demo-reviewer)")
        replay(Scenario("write-needs-flag", "Approved, but no --allow-irreversible -> approval request",
                        f'cua replay {orel} {p} -p nickname="Vacation Fund"', "needs_human"),
               opened, {**WRITE, "nickname": "Vacation Fund"})
        replay(Scenario("write-success", "Approved + explicit flag -> sub-account opened",
                        f'cua replay {orel} {p} -p nickname="Vacation Fund" --allow-irreversible', "success"),
               opened, {**WRITE, "nickname": "Vacation Fund"}, allow_irreversible=True)
        replay(Scenario("write-idempotent", "Same request again -> probe finds it, nothing re-submitted",
                        f'cua replay {orel} {p} -p nickname="Vacation Fund" --allow-irreversible', "success"),
               opened, {**WRITE, "nickname": "Vacation Fund"}, allow_irreversible=True)
        replay(Scenario("write-validation", "Business outcome: app rejects the deposit, with field details",
                        f'cua replay {orel} -p initial_deposit=2.00 ... --allow-irreversible', "business_outcome"),
               opened, {**WRITE, "initial_deposit": "2.00", "nickname": "Tiny"}, allow_irreversible=True)

    print("== multi-tenant: same artifact, tenant 'lakeshore' (relabeled, /tb prefix, extra column, v4.4)")
    ov = TenantOverride.model_validate(yaml.safe_load((root / "config/tenants/lakeshore.yaml").read_text()))
    v11 = store.add_tenant_override(artifacts, lookup, "lakeshore", ov, "lakeshore.yaml")
    v12 = store.add_tenant_override(artifacts, v11, "lakeshore_unmapped", TenantOverride(route_prefix="/tb"), "lakeshore_unmapped.yaml")
    r12 = v12.relative_to(root)
    replay(Scenario("tenant-override", "Tenant B via per-tenant override (no re-recording)",
                    f"cua replay {r12} -p member_id=10042 --tenant lakeshore", "success"),
           v12, {"member_id": "10042"}, tenant="lakeshore")
    replay(Scenario("tenant-drift", "Tenant B with relabels unmapped -> drift signals + precise failure",
                    f"cua replay {r12} -p member_id=10042 --tenant lakeshore_unmapped --escalation fail", "failure"),
           v12, {"member_id": "10042"}, tenant="lakeshore_unmapped", escalation="fail")

    # ---- package evidence
    (evidence_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    for p in artifacts.glob("mockcore.*/*.json"):
        shutil.copy(p, evidence_dir / "artifacts" / f"{p.parent.name}-{p.stem}.json")
    _summary(evidence_dir, scenarios)
    ok = all(s.ok for s in scenarios)
    print(f"\n{sum(s.ok for s in scenarios)}/{len(scenarios)} scenarios as expected. Evidence: {evidence_dir}/SUMMARY.md")
    return 0 if ok else 1


def _summary(evidence_dir: Path, scenarios: list[Scenario]) -> None:
    lines = [
        "# Demo evidence",
        "",
        "Generated by `cua demo` (MockLLM + scripted operator, synthetic data). Each row links to the run",
        "directory: `run.jsonl` is the structured, redacted log; `result.json` the typed result; `snapshots/`",
        "holds masked screenshots and redacted accessibility snapshots; `interventions/` the handoff requests",
        "and operator decisions; `session.json` the control-lock history. Playwright traces are written to",
        "`restricted/` and are deliberately not committed (they contain unredacted DOM).",
        "",
        "| # | Scenario | Expected | Got | Run | Key result (redacted) |",
        "|---|---|---|---|---|---|",
    ]
    for i, s in enumerate(scenarios, 1):
        summary = s.summary.replace("|", "\\|")
        if len(summary) > 400:
            summary = summary[:400] + "…"
        lines.append(f"| {i} | **{s.sid}**: {s.title} | `{s.expected}` | `{s.status}` {'✅' if s.ok else '❌'} | "
                     f"[{Path(s.run_dir).name}]({s.run_dir}/) | `{summary}` |")
    lines += ["", "## Equivalent commands", ""]
    lines += [f"- **{s.sid}**: `{s.command}`" for s in scenarios]
    lines += ["", "Saved artifacts are copied to [`artifacts/`](artifacts/)."]
    (evidence_dir / "SUMMARY.md").write_text("\n".join(lines) + "\n")

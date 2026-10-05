"""cua - command line entry point.

    cua target-app                      start the mock bank (http://localhost:8000)
    cua discover --goal "..." --target http://localhost:8000 [--mock-llm]
    cua replay artifacts/<id>/<ver>.json --param member_id=10042 [--inject not_found]
    cua approve artifacts/<id>/<ver>.json --reviewer alice
    cua operator                        operator UI for interventions (http://localhost:8001)
    cua demo                            scripted end-to-end run that populates evidence/
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import typer

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Computer-use automation: discover -> artifact -> deterministic replay.")

ROOT = Path.cwd()
POLICY = Path("config/policy.yaml")
PROFILE = Path("config/apps/mockcore.yaml")


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader (no extra dependency). Never overrides real environment variables."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if v.strip():
            os.environ.setdefault(k.strip(), v.strip())


def _policy(path: Path):
    from cua.policy.gate import PolicyConfig

    return PolicyConfig.load(path)


def inject_condition(base_url: str, preset: str | None) -> None:
    import httpx

    httpx.post(f"{base_url}/__admin/conditions", json={"preset": preset} if preset else {}).raise_for_status()


def reset_app(base_url: str) -> None:
    import httpx

    httpx.post(f"{base_url}/__admin/reset").raise_for_status()


@app.callback()
def _main() -> None:
    load_dotenv()


@app.command("target-app")
def target_app(port: int = 8000, host: str = "127.0.0.1") -> None:
    """Start the synthetic legacy bank back-office app."""
    import uvicorn

    uvicorn.run("target_app.app:app", host=host, port=port, log_level="warning")


@app.command()
def discover(
    goal: str = typer.Option(..., help="Natural-language goal"),
    target: str = typer.Option("http://localhost:8000", help="App URL or entry point (default entry: /app)"),
    mock_llm: bool = typer.Option(False, "--mock-llm", help="Use the scripted MockLLM (no API key needed)"),
    headed: bool = typer.Option(False, help="Show the browser window"),
    escalation: str = typer.Option("wait", help="wait: block for an operator on handoff; detach: stop with needs_human"),
    handoff_timeout: float = typer.Option(300, help="Seconds to wait for an operator"),
    max_steps: int | None = typer.Option(None),
    policy: Path = POLICY,
    profile: Path = PROFILE,
    runs_dir: Path = Path("runs"),
    artifacts_dir: Path = Path("artifacts"),
) -> None:
    """LLM-driven discovery run; on success writes a draft capability artifact."""
    from cua.agent.loop import DiscoveryOptions, DiscoveryRunner
    from cua.artifact.profile import AppProfile

    if target.rstrip("/").count("/") <= 2:
        target = target.rstrip("/") + "/app"
    if mock_llm:
        from cua.agent.mock_llm import MockLLM

        llm = MockLLM()
    else:
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            typer.secho("No ANTHROPIC_API_KEY set. Use --mock-llm to run offline.", fg="red")
            raise typer.Exit(2)
        from cua.agent.llm import AnthropicLLM

        llm = AnthropicLLM()
    runner = DiscoveryRunner(llm, _policy(policy), AppProfile.load(profile), DiscoveryOptions(
        max_steps=max_steps, headed=headed, escalation=escalation, handoff_timeout_s=handoff_timeout,
        runs_root=runs_dir, artifacts_root=artifacts_dir))
    r = runner.run(goal, target)
    color = "green" if r.stop_reason == "goal_met" else "yellow"
    typer.secho(f"stop_reason: {r.stop_reason}  ({r.detail})", fg=color)
    typer.echo(f"run log:     {r.run_dir / 'run.jsonl'}")
    if r.artifact_path:
        typer.echo(f"artifact:    {r.artifact_path}  ({r.capability_id} v{r.version}, draft)")
    raise typer.Exit(0 if r.stop_reason == "goal_met" else 1)


def _parse_params(params: list[str]) -> dict[str, str]:
    out = {}
    for p in params:
        if "=" not in p:
            raise typer.BadParameter(f"--param expects name=value, got {p!r}")
        k, v = p.split("=", 1)
        out[k] = v
    return out


@app.command()
def replay(
    artifact: Path = typer.Argument(..., exists=True),
    param: list[str] = typer.Option([], "--param", "-p", help="name=value (repeatable)"),
    inject: str | None = typer.Option(None, help="not_found|validation|permission|interstitial|session_expiry|slow|500|500_persistent|unknown_modal"),
    tenant: str | None = typer.Option(None, help="apply this tenant's overrides from the artifact"),
    base_url: str = typer.Option("http://localhost:8000"),
    allow_irreversible: bool = typer.Option(False, "--allow-irreversible"),
    escalation: str = typer.Option("detach", help="fail | detach (return needs_human) | wait (block for operator)"),
    handoff_timeout: float = typer.Option(300),
    headed: bool = False,
    reveal: bool = typer.Option(False, help="print raw outputs (the persisted result is always redacted)"),
    policy: Path = POLICY,
    runs_dir: Path = Path("runs"),
) -> None:
    """Deterministic replay of an artifact. No LLM is involved."""
    from cua.artifact import store
    from cua.replay.engine import ReplayEngine, ReplayOptions

    cap = store.load(artifact)
    if inject:
        inject_condition(base_url, inject)
    engine = ReplayEngine(cap, _policy(policy), ReplayOptions(
        base_url=base_url, tenant=tenant, allow_irreversible=allow_irreversible, escalation=escalation,
        handoff_timeout_s=handoff_timeout, headed=headed, runs_root=runs_dir))
    result = engine.run(_parse_params(param))
    if inject:
        inject_condition(base_url, None)
    shown = result.model_dump(mode="json", exclude={"events"})
    if not reveal:
        shown = json.loads((Path(result.evidence_dir) / "result.json").read_text())
        shown.pop("events", None)
    typer.echo(json.dumps(shown, indent=2))
    raise typer.Exit({"success": 0, "business_outcome": 0, "needs_human": 3}.get(result.status, 1))


@app.command()
def approve(artifact: Path = typer.Argument(..., exists=True), reviewer: str = typer.Option(...), note: str | None = None) -> None:
    """Mark a reviewed draft artifact as approved (required for unattended irreversible replay)."""
    from cua.artifact import store

    cap = store.approve(artifact, reviewer, note)
    typer.secho(f"{cap.capability_id} v{cap.version} approved by {reviewer}", fg="green")


@app.command()
def override(
    artifact: Path = typer.Argument(..., exists=True),
    tenant: str = typer.Option(..., help="tenant key, e.g. lakeshore"),
    file: Path = typer.Option(..., exists=True, help="YAML TenantOverride"),
    artifacts_dir: Path = Path("artifacts"),
) -> None:
    """Attach a per-tenant override to a capability; writes a new MINOR version (back to draft)."""
    import yaml

    from cua.artifact import store
    from cua.artifact.schema import TenantOverride

    ov = TenantOverride.model_validate(yaml.safe_load(file.read_text()))
    path = store.add_tenant_override(artifacts_dir, artifact, tenant, ov, file.name)
    cap = store.load(path)
    typer.secho(f"{cap.capability_id} v{cap.version} (parent {cap.provenance.parent_version}) -> {path}", fg="green")


@app.command()
def operator(port: int = 8001, runs_dir: Path = Path("runs")) -> None:
    """Minimal operator UI: list interventions, take control, approve, resume, abort."""
    import uvicorn

    os.environ["CUA_RUNS_DIRS"] = str(runs_dir)
    uvicorn.run("operator_ui.app:app", host="127.0.0.1", port=port, log_level="warning")


@app.command()
def decide(
    request_dir: Path = typer.Argument(..., exists=True, help="runs/<run>/interventions/<id>"),
    decision: str = typer.Argument(..., help="take_control | approve | resume | abort"),
    operator_name: str = typer.Option("cli-operator", "--operator"),
    note: str = "",
) -> None:
    """CLI equivalent of the operator UI buttons."""
    from cua.handoff.interventions import submit_decision

    submit_decision(request_dir, decision, operator_name, note)  # type: ignore[arg-type]
    typer.echo(f"{decision} -> {request_dir}")


@app.command()
def inject(preset: str = typer.Argument(..., help="condition preset, or 'reset'"), base_url: str = "http://localhost:8000") -> None:
    """Toggle a runtime condition on the mock app (test harness)."""
    reset_app(base_url) if preset == "reset" else inject_condition(base_url, preset)
    typer.echo(f"ok: {preset}")


@app.command()
def schema(out: Path = Path("schemas/capability.schema.json")) -> None:
    """Export the artifact JSON Schema."""
    from cua.artifact.store import export_schema

    export_schema(out)
    typer.echo(f"wrote {out}")


@app.command()
def demo(
    evidence_dir: Path = Path("evidence"),
    base_url: str = "http://localhost:8000",
    start_app: bool = typer.Option(True, help="start the mock app in-process if it's not running"),
    keep_existing: bool = typer.Option(False, help="don't wipe evidence/ first"),
) -> None:
    """Scripted end-to-end run (MockLLM, scripted operator). Populates evidence/."""
    from cua.demo import run_demo

    sys.exit(run_demo(evidence_dir, base_url, start_app=start_app, keep_existing=keep_existing))


if __name__ == "__main__":
    app()

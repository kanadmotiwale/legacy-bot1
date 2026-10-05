"""PolicyGate decisions and the control-lock state machine."""

from __future__ import annotations

import pytest

from cua.artifact.schema import RiskClass, Status
from cua.handoff.lock import ControlLock, GuardedSurface, IllegalTransition, LockViolation, SessionState
from cua.policy.gate import ActionContext, Decision, PolicyGate
from cua.surface.types import ActionRequest, ActionResult

APP = "http://localhost:8000"


@pytest.fixture()
def gate(policy):
    return PolicyGate(policy)


def ctx(**kw):
    base = dict(mode="replay", action="click", frame_url=f"{APP}/member/search", target_role="button", target_name="Search")
    return ActionContext(**{**base, **kw})


def test_allowlist(gate):
    assert gate.evaluate(ctx()).decision == Decision.ALLOW
    assert gate.evaluate(ctx(action="navigate", url=f"{APP}/__admin/reset")).decision == Decision.BLOCK
    assert gate.evaluate(ctx(action="navigate", url="https://evil.example.com/app")).decision == Decision.BLOCK
    assert gate.evaluate(ctx(action="click", url="https://evil.example.com/x", target_role="link")).decision == Decision.BLOCK
    assert gate.evaluate(ctx(action="execute_js")).decision == Decision.BLOCK


def test_risk_classification_is_policys_job(gate):
    submit = dict(frame_url=f"{APP}/subaccount/review", target_name="Submit")
    assert gate.classify(ctx(**submit))[0] == RiskClass.IRREVERSIBLE
    assert gate.classify(ctx(target_name="Back", frame_url=f"{APP}/subaccount/review"))[0] == RiskClass.REVERSIBLE
    # an artifact can raise risk but never lower it
    assert gate.classify(ctx(declared_risk=RiskClass.IRREVERSIBLE))[0] == RiskClass.IRREVERSIBLE
    assert gate.classify(ctx(declared_risk=RiskClass.SAFE, **submit))[0] == RiskClass.IRREVERSIBLE


def test_irreversible_discovery_requires_approval(gate):
    submit = dict(mode="discovery", frame_url=f"{APP}/subaccount/review", target_name="Submit")
    assert gate.evaluate(ctx(**submit)).decision == Decision.REQUIRES_APPROVAL
    assert gate.evaluate(ctx(**submit, approved_by_human=True)).decision == Decision.ALLOW


def test_irreversible_replay_gates(gate):
    submit = dict(frame_url=f"{APP}/subaccount/review", target_name="Submit", capability_id="mockcore.subaccount.open")
    assert gate.evaluate(ctx(**submit, artifact_status=Status.DRAFT, allow_irreversible=True)).decision == Decision.BLOCK
    assert gate.evaluate(ctx(**submit, artifact_status=Status.APPROVED)).decision == Decision.REQUIRES_APPROVAL
    assert gate.evaluate(ctx(**submit, artifact_status=Status.APPROVED, allow_irreversible=True)).decision == Decision.ALLOW
    other = {**submit, "capability_id": "mockcore.something.else"}
    assert gate.evaluate(ctx(**other, artifact_status=Status.APPROVED, allow_irreversible=True)).decision == Decision.BLOCK


# ------------------------------------------------------------------ control lock


def test_happy_handoff_cycle(tmp_path):
    lock = ControlLock("r1", tmp_path / "session.json")
    assert lock.owner == "automation" and lock.epoch == 1
    lock.transition("escalate", "automation")
    assert lock.owner == "none"
    lock.transition("take_control", "human:alice")
    assert lock.owner == "human"
    lock.transition("resume", "human:alice")
    lock.transition("verified", "automation")
    assert lock.state == SessionState.RUNNING and lock.epoch == 2
    assert (tmp_path / "session.json").exists()


@pytest.mark.parametrize("path", [
    ["take_control"],                      # can't take control of a running session without a pause
    ["resume"],                            # nothing to resume
    ["escalate", "resume"],                # must take control (or approve) first
    ["escalate", "verified"],
    ["escalate", "take_control", "approve"],
    ["escalate", "abort", "resume"],       # terminal
    ["complete", "escalate"],
])
def test_illegal_transitions(path):
    lock = ControlLock("r")
    with pytest.raises(IllegalTransition):
        for ev in path:
            lock.transition(ev, "x")


class Inner:
    def act(self, action):
        return ActionResult(ok=True)


def test_automation_cannot_act_without_the_lock():
    lock = ControlLock("r")
    guard = GuardedSurface(Inner(), lock)
    assert guard.act(ActionRequest(kind="navigate", value="/")).ok
    lock.transition("escalate", "automation")
    lock.transition("take_control", "human")
    with pytest.raises(LockViolation):
        guard.act(ActionRequest(kind="navigate", value="/"))


def test_stale_fencing_token_rejected():
    lock = ControlLock("r")
    guard = GuardedSurface(Inner(), lock)
    for ev in ("escalate", "take_control", "resume", "verified"):
        lock.transition(ev, "x")
    with pytest.raises(LockViolation, match="stale"):
        guard.act(ActionRequest(kind="navigate", value="/"))  # token from before the handoff
    guard.refresh_token()
    assert guard.act(ActionRequest(kind="navigate", value="/")).ok

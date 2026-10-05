"""Deterministic replay: execute a capability artifact with typed params. No LLM.

Per step:  detectors (pre) -> precondition -> resolve target -> policy -> act
           -> wait for (detectors | checkpoint) -> handle detector or verify checkpoint
Detectors are evaluated before the checkpoint every time. All waits are explicit
condition waits with a deadline; the only timed delay is retry backoff.

This module (and everything under cua.replay) must never import cua.agent or anthropic;
tests/test_replay_no_llm.py enforces it.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin

from cua.artifact.binding import bind
from cua.artifact.params import normalize, validate_inputs
from cua.artifact.schema import (
    Capability,
    Condition,
    Dismiss,
    ElementVisible,
    Escalate,
    Fail,
    FieldValue,
    Handler,
    HttpStatus,
    Reauthenticate,
    Retry,
    ReturnOutcome,
    RiskClass,
    RouteMatches,
    Sensitivity,
    Status,
    Step,
    TextVisible,
)
from cua.evidence.log import RunLog, capture, new_run_id
from cua.handoff.coordinator import HandoffCoordinator
from cua.handoff.lock import ControlLock, GuardedSurface, SessionState
from cua.policy.gate import ActionContext, CredentialResolver, Decision, PolicyConfig, PolicyGate
from cua.policy.redaction import Redactor
from cua.replay.resolve import resolve_target
from cua.replay.result import (
    BusinessOutcome,
    DriftSignal,
    Failure,
    NeedsHuman,
    ReplayEvent,
    ReplayResult,
    Success,
)
from cua.surface.types import ActionRequest

EVENT_KINDS = {
    "login", "step_completed", "handler_fired", "dismissed", "retry", "reauthenticated", "drift",
    "intervention_requested", "operator_decision", "human_action", "skip_ahead", "resume_verified",
    "idempotency_probe", "policy", "extracted", "version_check",
}


@dataclass
class ReplayOptions:
    base_url: str = "http://localhost:8000"
    tenant: str | None = None
    allow_irreversible: bool = False
    escalation: Literal["fail", "detach", "wait"] = "detach"
    handoff_timeout_s: float = 300
    headed: bool = False
    trace: bool = True
    runs_root: Path = field(default_factory=lambda: Path("runs"))


class _Stop(Exception):
    def __init__(self, result: ReplayResult) -> None:
        self.result = result


class _Restart(Exception):
    pass


class _Goto(Exception):
    def __init__(self, index: int) -> None:
        self.index = index


def describe(conds: list[Condition]) -> str:
    parts = []
    for c in conds:
        if isinstance(c, RouteMatches):
            parts.append(f"route matches {c.pattern}")
        elif isinstance(c, TextVisible):
            parts.append(f"text '{c.text}' visible")
        elif isinstance(c, ElementVisible):
            parts.append(f"{c.target.description} visible")
        elif isinstance(c, FieldValue):
            parts.append(f"{c.target.description} == '{c.equals}'")
        elif isinstance(c, HttpStatus):
            parts.append(f"HTTP status {c.min}-{c.max}")
    return " AND ".join(parts) or "(no condition)"


def version_in_range(version: str, spec: str) -> bool:
    v = tuple(int(x) for x in version.split("."))
    for clause in spec.split(","):
        m = re.fullmatch(r"\s*(>=|<=|>|<|==)\s*([\d.]+)\s*", clause)
        if not m:
            continue
        op, bound = m.groups()
        b = tuple(int(x) for x in bound.split("."))
        n = max(len(v), len(b))
        vv, bb = v + (0,) * (n - len(v)), b + (0,) * (n - len(b))
        if not {">=": vv >= bb, "<=": vv <= bb, ">": vv > bb, "<": vv < bb, "==": vv == bb}[op]:
            return False
    return True


class ReplayEngine:
    def __init__(self, capability: Capability, policy: PolicyConfig, options: ReplayOptions | None = None) -> None:
        self.cap = capability
        self.policy = policy
        self.gate = PolicyGate(policy)
        self.opt = options or ReplayOptions()

    # ================================================================== entry point
    def run(self, raw_params: dict[str, str]) -> ReplayResult:
        self.run_id = new_run_id("replay")
        self.run_dir = self.opt.runs_root / self.run_id
        self.redactor = Redactor(self.policy.redaction)
        self.creds = CredentialResolver(self.policy, self.redactor)
        for ref in self.policy.credentials:  # pre-register so secrets are scrubbed even if never typed
            try:
                self.creds.get(ref)
            except KeyError:
                pass
        self.log = RunLog(self.run_dir, self.run_id, self.redactor)
        self.drift: list[DriftSignal] = []
        self.outputs: dict[str, Any] = {}
        self.counts: dict[str, int] = {}
        self.t0 = time.monotonic()
        self.browser = None
        self.log.write_json(
            "meta.json",
            {"kind": "replay", "capability_id": self.cap.capability_id, "version": self.cap.version, "tenant": self.opt.tenant, "started_at": time.time()},
        )

        params, errors = validate_inputs(self.cap.inputs, raw_params)
        for p in self.cap.inputs:
            if p.name in params and p.sensitivity == Sensitivity.PII:
                self.redactor.register(params[p.name], p.name, partial=True)
        self.params = params
        self.log.event(
            "replay_started",
            capability_id=self.cap.capability_id,
            version=self.cap.version,
            fingerprint=self.cap.fingerprint,
            status=self.cap.status,
            tenant=self.opt.tenant,
            params=params,
            options={"allow_irreversible": self.opt.allow_irreversible, "escalation": self.opt.escalation},
        )
        try:
            result = self._run_validated(errors)
        except _Stop as s:
            result = s.result
        return self._finish(result)

    def _run_validated(self, errors) -> ReplayResult:
        if errors:  # never touch the UI with bad input
            return Failure(**self._base(), error_code="invalid_input", message="input validation failed",
                           expected="params matching the capability's typed inputs",
                           observed="; ".join(f"{e.name}: {e.message}" for e in errors))
        if self.cap.status == Status.DEPRECATED:
            return Failure(**self._base(), error_code="policy_blocked", message="artifact is deprecated")
        ok, why = self.gate.check_rate(self.cap.capability_id, self.opt.runs_root)
        if not ok:
            return Failure(**self._base(), error_code="rate_limited", message=why)
        try:
            bound, override = bind(self.cap, self.opt.tenant, self.params)
        except KeyError as e:
            return Failure(**self._base(), error_code="invalid_input", message=str(e))
        self.base_url = override.base_url or self.opt.base_url

        from cua.surface.browser import BrowserSession  # imported lazily: keeps unit tests browser-free
        from cua.surface.playwright_web import PlaywrightWebSurface

        self.browser = BrowserSession(self.run_dir, headed=self.opt.headed, trace=self.opt.trace)
        page = self.browser.start()
        self.surface = PlaywrightWebSurface(
            page, mask_rules={"log": self.redactor.mask_rules("log"), "llm": self.redactor.mask_rules("llm")}
        )
        self.lock = ControlLock(self.run_id, self.run_dir / "session.json")
        self.guard = GuardedSurface(self.surface, self.lock)
        self.coord = HandoffCoordinator(
            run_dir=self.run_dir, lock=self.lock, log=self.log, surface=self.surface, mode="replay",
            cdp_endpoint=self.browser.cdp_endpoint,
            timeout_s=self.opt.handoff_timeout_s if self.opt.escalation == "wait" else 0,
            bring_to_front=page.bring_to_front,
        )
        self.browser.on_human_event(self.coord.on_human_event)
        try:
            return self._execute(bound)
        except _Stop as s:
            return s.result
        except Exception as e:  # noqa: BLE001 - turn anything unexpected into a debuggable Failure
            return Failure(**self._base(), error_code="internal_error", message=f"{type(e).__name__}: {e}",
                           evidence=capture(self.surface, self.log, "internal-error"))

    def _finish(self, result: ReplayResult) -> ReplayResult:
        if self.browser is not None:
            if self.lock.state == SessionState.RUNNING:
                self.lock.transition("complete" if result.status in ("success", "business_outcome") else "fail", "automation")
            trace = self.browser.close()
            if trace:
                self.log.event("trace_saved", path=str(trace.relative_to(self.run_dir)), note="restricted: unredacted DOM, not for sharing")
        result.duration_ms = int((time.monotonic() - self.t0) * 1000)
        result.drift = self.drift
        result.evidence_dir = str(self.run_dir)
        result.events = self._events()
        self.log.event("replay_finished", status=result.status, result=result.model_dump(mode="json", exclude={"events"}))
        self.log.write_json("result.json", result.model_dump(mode="json"))
        self.log.finalize()
        return result

    def _events(self) -> list[ReplayEvent]:
        out = []
        for line in self.log.path.read_text().splitlines():
            rec = json.loads(line)
            if rec["type"] in EVENT_KINDS:
                detail = {k: v for k, v in rec.items() if k not in ("run_id", "seq", "t_ms", "ts", "type", "step_id")}
                out.append(ReplayEvent(t_ms=rec["t_ms"], kind=rec["type"], step_id=rec.get("step_id"), detail=detail))
        return out

    def _base(self) -> dict:
        return {"run_id": self.run_id, "capability_id": self.cap.capability_id, "version": self.cap.version, "tenant": self.opt.tenant}

    # ================================================================== main flow
    def _execute(self, cap: Capability) -> ReplayResult:
        self._login(cap)
        self._check_version(cap)
        if cap.idempotency and self._probe(cap, when="before_start"):
            return Success(**self._base(), outputs={}, already_existed=True)
        self._goto_entry(cap)
        i, restarts = 0, 0
        while i < len(cap.steps):
            try:
                i = self._run_step(cap, i)
            except _Goto as g:
                i = g.index
            except _Restart:
                restarts += 1
                if restarts > 2:
                    raise _Stop(Failure(**self._base(), error_code="reauth_failed", message="session lost repeatedly"))
                self._goto_entry(cap)
                i = 0
        if not self._wait_all(cap.success, 5000):
            raise _Stop(self._failure(cap, None, "checkpoint_failed", "final success condition not met", describe(cap.success)))
        missing = [o.name for o in cap.outputs if o.required and o.name not in self.outputs]
        if missing:
            raise _Stop(self._failure(cap, None, "extraction_failed", f"outputs not extracted: {missing}", "all required outputs"))
        return Success(**self._base(), outputs=self.outputs)

    def _goto_entry(self, cap: Capability) -> None:
        self.guard.act(ActionRequest(kind="navigate", value=urljoin(self.base_url, cap.app.entry_point)))
        if cap.session and not self._wait_all(cap.session.logged_in, 10_000):
            raise _Stop(self._failure(cap, None, "reauth_failed", "not signed in at entry point", describe(cap.session.logged_in)))

    def _run_step(self, cap: Capability, i: int) -> int:
        step = cap.steps[i]
        t0 = time.monotonic()
        handlers = [h for h in cap.handlers if h.applies(step.id)]
        self.log.event("step_started", step_id=step.id, index=i, action=step.action, description=step.description, risk=step.risk)
        self._settle_detectors(cap, step, i, handlers)
        if step.precondition and not self._wait_all(step.precondition, step.timeout_ms, cap, step, i, handlers):
            self._unrecognized(cap, step, i, describe(step.precondition), "precondition not met")
        if step.action == "extract":
            self._extract(cap, step, i, handlers)
        else:
            self._act(cap, step, i, handlers)
        self.log.event("step_completed", step_id=step.id, index=i, duration_ms=int((time.monotonic() - t0) * 1000))
        return i + 1

    # ================================================================== actions
    def _act(self, cap: Capability, step: Step, i: int, handlers: list[Handler]) -> None:
        for attempt in range(1, 4):
            pre_ok = bool(step.checkpoint) and all(self.surface.check(c, self.params) for c in step.checkpoint)
            navs0 = self.surface.nav_count()
            req = self._prepare(cap, step, i, handlers)
            r = self.guard.act(req)
            self.log.event("action", step_id=step.id, kind=req.kind, ok=r.ok, error=r.error, duration_ms=r.duration_ms)
            if not r.ok:
                if self._first_detector(handlers):
                    self._settle_detectors(cap, step, i, handlers)
                    continue
                raise _Stop(self._failure(cap, step, "action_failed", r.error or "action failed", f"{step.action} on {step.target.description if step.target else step.value}"))
            if self._post_wait(cap, step, i, handlers, pre_ok, navs0) != "rerun":
                return
        raise _Stop(self._failure(cap, step, "action_failed", "step could not be completed after re-runs", describe(step.checkpoint)))

    def _prepare(self, cap: Capability, step: Step, i: int, handlers: list[Handler]) -> ActionRequest:
        if step.action == "navigate":
            url = urljoin(self.base_url, step.value)
            self._policy(cap, step, i, {"url": url, "frame_url": url})
            return ActionRequest(kind="navigate", value=url, timeout_ms=step.timeout_ms)
        target = self._resolve(cap, step, i, handlers)
        info = self.surface.describe_target(target)
        self._policy(cap, step, i, {"url": info.get("href") if info.get("role") == "link" else None,
                                    "frame_url": info.get("frame_url"), "target_role": info.get("role"),
                                    "target_name": info.get("name")})
        value = self.creds.get(step.secret_ref) if step.secret_ref else step.value
        return ActionRequest(kind=step.action, target=target, value=value, timeout_ms=step.timeout_ms)

    def _resolve(self, cap: Capability, step: Step, i: int, handlers: list[Handler]):
        for _ in range(4):
            res = resolve_target(self.surface, step.target, step.timeout_ms, interrupt=lambda: self._first_detector(handlers) is not None)
            if res.interrupted:
                self._settle_detectors(cap, step, i, handlers)
                continue
            break
        if res.target is None:
            if self._first_detector(handlers):
                self._settle_detectors(cap, step, i, handlers)
            code = "target_ambiguous" if res.ambiguous else "target_not_found"
            raise _Stop(self._failure(cap, step, code, f"no unique match for '{step.target.description}'",
                                      f"exactly one element for any of {[c.strategy for c in step.target.candidates]}",
                                      attempts=res.attempts))
        cand = step.target.candidates[res.index]
        self.log.event("locator_resolved", step_id=step.id, strategy=cand.strategy, index=res.index,
                       attempts=[a.model_dump() for a in res.attempts])
        if res.index > 0:
            sig = DriftSignal(step_id=step.id, expected_strategy=step.target.candidates[0].strategy,
                              matched_strategy=cand.strategy, matched_index=res.index)
            self.drift.append(sig)
            self.log.event("drift", step_id=step.id, **sig.model_dump(exclude={"step_id"}))
        return res.target

    def _policy(self, cap: Capability, step: Step, i: int, info: dict, approved: bool = False) -> None:
        ctx = ActionContext(mode="replay", action=step.action, capability_id=cap.capability_id,
                            artifact_status=self.cap.status, declared_risk=step.risk,
                            allow_irreversible=self.opt.allow_irreversible, approved_by_human=approved, **info)
        d = self.gate.evaluate(ctx)
        self.log.event("policy", step_id=step.id, decision=d.decision, reason=d.reason, risk=d.risk, rule=d.rule)
        if d.decision == Decision.BLOCK:
            raise _Stop(self._failure(cap, step, "policy_blocked", d.reason, "an action permitted by policy"))
        if d.decision == Decision.REQUIRES_APPROVAL:
            outcome = self._escalate(cap, step, i, "approval", d.reason,
                                     proposed={"action": step.action, "target": step.target.description if step.target else step.value, "risk": d.risk})
            if outcome == "approved":
                self._policy(cap, step, i, info, approved=True)

    def _extract(self, cap: Capability, step: Step, i: int, handlers: list[Handler]) -> None:
        target = self._resolve(cap, step, i, handlers)
        self._policy(cap, step, i, {"frame_url": self.surface.describe_target(target).get("frame_url")})
        raw = self.surface.read(target.frame_path, target.locator)
        out = cap.output(step.output)
        try:
            value = normalize(raw, out.normalize, out.type)
        except Exception:  # noqa: BLE001
            raise _Stop(self._failure(cap, step, "extraction_failed", f"could not normalize {out.name}",
                                      f"{out.type} via {out.normalize}", observed_override=self.redactor.text(raw)))
        if out.sensitivity in (Sensitivity.PII, Sensitivity.SECRET):
            self.redactor.register(str(value), out.name)
            self.redactor.register(raw.strip(), out.name)
        self.outputs[out.name] = value
        self.log.event("extracted", step_id=step.id, output=out.name, value=str(value),
                       digest="sha256:" + hashlib.sha256(str(value).encode()).hexdigest()[:12])

    # ================================================================== waiting + detectors
    def _first_detector(self, handlers: list[Handler]) -> Handler | None:
        for h in handlers:
            if all(self.surface.check(c, self.params) for c in h.detect):
                return h
        return None

    def _settle_detectors(self, cap: Capability, step: Step | None, i: int, handlers: list[Handler]) -> None:
        """Handle any detector that currently matches (e.g. an interstitial already showing)."""
        for _ in range(5):
            h = self._first_detector(handlers)
            if h is None:
                return
            self._handle(cap, h, step, i)

    def _wait_all(self, conds: list[Condition], timeout_ms: int, cap=None, step=None, i=0, handlers=None) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            if handlers and (h := self._first_detector(handlers)):
                self._handle(cap, h, step, i)
            if all(self.surface.check(c, self.params) for c in conds):
                return True
            if time.monotonic() >= deadline:
                return False
            self.surface.wait(100)

    def _post_wait(self, cap, step: Step, i: int, handlers: list[Handler], pre_ok: bool, navs0: int) -> str:
        deadline = time.monotonic() + step.timeout_ms / 1000
        while True:
            h = self._first_detector(handlers)
            if h is not None:
                if self._handle(cap, h, step, i) == "rerun":
                    return "rerun"
                deadline = time.monotonic() + step.timeout_ms / 1000
                continue
            changed = (not pre_ok) or self.surface.nav_count() > navs0 or step.action in ("type", "select")
            if changed and all(self.surface.check(c, self.params) for c in step.checkpoint):
                return "ok"
            if time.monotonic() >= deadline:
                break
            self.surface.wait(100)
        if step.risk == RiskClass.IRREVERSIBLE:
            self._ambiguous_after_submit(cap, step, i, "checkpoint not reached after submit")
        self._unrecognized(cap, step, i, describe(step.checkpoint), "checkpoint not reached and no known condition detected")
        return "ok"

    def _handle(self, cap: Capability, h: Handler, step: Step | None, i: int) -> str:
        sid = step.id if step else None
        resp = h.response
        self.log.event("handler_fired", step_id=sid, handler=h.id, category=h.category, response=resp.type)
        if isinstance(resp, ReturnOutcome):
            details = {}
            for d in resp.details:
                lines = self.surface.read_lines_after(d.anchor_text, d.frame_path)
                if d.parse == "field_colon_message":
                    details[d.name] = {ln.split(":", 1)[0].strip(): ln.split(":", 1)[1].strip() for ln in lines if ":" in ln}
                else:
                    details[d.name] = lines
            raise _Stop(BusinessOutcome(**self._base(), outcome=resp.outcome, step_id=sid, details=details))
        if step is not None and step.risk == RiskClass.IRREVERSIBLE and isinstance(resp, (Retry, Reauthenticate)):
            self._ambiguous_after_submit(cap, step, i, f"{h.id} after irreversible step")
        n = self.counts[h.id] = self.counts.get(h.id, 0) + 1
        if isinstance(resp, Dismiss):
            if n > resp.max_times:
                self._unrecognized(cap, step, i, "interstitial dismissed", f"{h.id} keeps reappearing ({n}x)")
            res = resolve_target(self.surface, resp.target, 3000)
            if res.target is None:
                self._unrecognized(cap, step, i, resp.target.description, f"cannot dismiss {h.id}")
            self.guard.act(ActionRequest(kind="click", target=res.target, timeout_ms=3000))
            self.log.event("dismissed", step_id=sid, handler=h.id, count=n)
            return "continue"
        if isinstance(resp, Retry):
            if n > resp.policy.max_attempts:
                raise _Stop(self._failure(cap, step, "app_error", f"{h.id}: still failing after {n - 1} retries",
                                          describe(step.checkpoint) if step else "page load"))
            backoff = resp.policy.backoff_ms[min(n - 1, len(resp.policy.backoff_ms) - 1)]
            self.log.event("retry", step_id=sid, handler=h.id, attempt=n, backoff_ms=backoff)
            self.surface.wait(backoff)  # deliberate backoff between retries, not a state wait
            return "continue" if self.surface.reload_failed_documents() else "rerun"
        if isinstance(resp, Reauthenticate):
            if not self.policy.replay.allow_reauth:
                self._escalate(cap, step, i, "unrecoverable", "session expired and policy forbids re-authentication")
            if n > resp.max_times:
                raise _Stop(self._failure(cap, step, "reauth_failed", "session expired again after re-authentication", "a live session"))
            self._login(cap)
            self.log.event("reauthenticated", step_id=sid, handler=h.id, note="restarting flow from step 0")
            raise _Restart()
        if isinstance(resp, Escalate):
            self._escalate(cap, step, i, "unrecoverable", resp.reason)
            return "continue"
        if isinstance(resp, Fail):
            raise _Stop(self._failure(cap, step, resp.error_code, h.description, describe(step.checkpoint) if step else ""))
        return "continue"

    # ================================================================== session, version, idempotency
    def _login(self, cap: Capability) -> None:
        spec = cap.session
        if spec is None:
            return
        self.guard.act(ActionRequest(kind="navigate", value=urljoin(self.base_url, spec.login_route)))
        for step in spec.login_steps:
            req = self._prepare(cap, step, -1, [])
            r = self.guard.act(req)
            if not r.ok or (step.checkpoint and not self._wait_all(step.checkpoint, step.timeout_ms)):
                raise _Stop(self._failure(cap, step, "reauth_failed", r.error or "sign-on failed", describe(step.checkpoint)))
        if not self._wait_all(spec.logged_in, 10_000):
            raise _Stop(self._failure(cap, None, "reauth_failed", "sign-on did not reach the workstation", describe(spec.logged_in)))
        self.log.event("login", route=spec.login_route, credential_refs=spec.credential_refs)

    def _check_version(self, cap: Capability) -> None:
        probe = cap.app.version_probe
        if probe is None:
            return
        version = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and version is None:
            m = re.search(probe.pattern, self.surface.frame_text(probe.frame_path))
            version = m.group(1) if m else None
            if version is None:
                self.surface.wait(100)
        ok = version is not None and version_in_range(version, cap.app.app_version_range)
        self.log.event("version_check", observed=version, expected=cap.app.app_version_range, ok=ok)
        if version is not None and not ok:
            raise _Stop(self._failure(cap, None, "app_version_mismatch", f"app reports {version}",
                                      cap.app.app_version_range))

    def _probe(self, cap: Capability, when: str) -> bool:
        idem = cap.idempotency
        for step in idem.probe_steps:
            self._policy(cap, step, -1, {"url": urljoin(self.base_url, step.value or ""), "frame_url": urljoin(self.base_url, step.value or "")})
            self.guard.act(ActionRequest(kind="navigate", value=urljoin(self.base_url, step.value)))
            if not self._wait_all(step.checkpoint, step.timeout_ms):
                raise _Stop(self._failure(cap, step, "idempotency_ambiguous", "could not run idempotency probe", describe(step.checkpoint)))
        found = all(self.surface.check(c, self.params) for c in idem.exists)
        self.log.event("idempotency_probe", when=when, key_inputs=idem.key_inputs, found=found)
        return found

    def _ambiguous_after_submit(self, cap: Capability, step: Step, i: int, why: str) -> None:
        """After an irreversible step, never blindly retry: check whether the effect exists."""
        self.log.event("ambiguous_submit", step_id=step.id, why=why)
        if cap.session and not all(self.surface.check(c, self.params) for c in cap.session.logged_in) and \
                self.surface.check(RouteMatches(pattern=cap.session.login_route), self.params):
            self._login(cap)
        if cap.idempotency and self._probe(cap, when="after_ambiguous_submit"):
            raise _Stop(Success(**self._base(), outputs=self.outputs, already_existed=True))
        self._escalate(cap, step, i, "unrecoverable", f"irreversible step outcome unknown ({why}); effect not found - not re-submitting")
        raise _Stop(self._failure(cap, step, "idempotency_ambiguous", why, describe(step.checkpoint)))

    # ================================================================== escalation
    def _unrecognized(self, cap, step: Step | None, i: int, expected: str, why: str) -> None:
        if self.opt.escalation == "fail":
            raise _Stop(self._failure(cap, step, "unrecognized_state", why, expected))
        self._escalate(cap, step, i, "stuck", f"{why}; expected {expected}")

    def _escalate(self, cap, step: Step | None, i: int, kind: str, reason: str, proposed: dict | None = None) -> str:
        outcome = self.coord.escalate(
            kind=kind, reason=reason, capability_id=self.cap.capability_id, step_id=step.id if step else None,
            step_index=i if i >= 0 else None, step_description=step.description if step else None,
            proposed_action=proposed, expected_after_resume=describe(step.checkpoint) if step else None,
        )
        if outcome.decision in ("detached", "timeout"):
            raise _Stop(NeedsHuman(**self._base(), intervention_id=outcome.intervention_id, reason=reason,
                                   step_id=step.id if step else None))
        if outcome.decision == "aborted":
            raise _Stop(Failure(**self._base(), error_code="aborted_by_operator", message=f"operator {outcome.operator} aborted",
                                step_id=step.id if step else None, step_index=i if i >= 0 else None))
        if outcome.decision == "approved":
            self.lock.transition("verified", "automation", "approval granted")
            self.guard.refresh_token()
            return "approved"
        raise _Goto(self._reconcile(cap, i))

    def _reconcile(self, cap: Capability, i: int) -> int:
        """After a human hands back: re-observe and find where we actually are."""
        self.log.event("resume_reverify", from_step=i)
        for j in range(len(cap.steps) - 1, max(i, 0) - 1, -1):
            s = cap.steps[j]
            if s.checkpoint and s.action in ("click", "navigate") and all(self.surface.check(c, self.params) for c in s.checkpoint):
                self.lock.transition("verified", "automation", f"checkpoint of {s.id} holds")
                self.guard.refresh_token()
                self.log.event("resume_verified", step_id=s.id, matched_checkpoint=describe(s.checkpoint), next_index=j + 1)
                if j + 1 != i:
                    self.log.event("skip_ahead", from_index=i, to_index=j + 1)
                return j + 1
        step = cap.steps[i] if i >= 0 else None
        if step and (not step.precondition or all(self.surface.check(c, self.params) for c in step.precondition)):
            if step.target is None or resolve_target(self.surface, step.target, 2000).target is not None:
                self.lock.transition("verified", "automation", f"precondition of {step.id} holds")
                self.guard.refresh_token()
                self.log.event("resume_verified", step_id=step.id, matched_checkpoint="precondition", next_index=i)
                return i
        self.lock.transition("fail", "automation", "state after handoff matches no known checkpoint")
        raise _Stop(self._failure(cap, step, "unrecognized_state", "after handoff the app is in no state this capability recognizes",
                                  describe(step.checkpoint) if step else ""))

    # ================================================================== failures
    def _failure(self, cap, step: Step | None, code: str, message: str, expected: str,
                 attempts=None, observed_override: str | None = None) -> Failure:
        loc = self.surface.current_location()
        observed = observed_override or "; ".join(
            f"{'/'.join(f.name or f.url_pattern or '?' for f in fr.path) or 'top'}={fr.url} [{fr.status or '-'}]" for fr in loc.frames
        )
        evidence = capture(self.surface, self.log, f"failure-{step.id if step else 'run'}")
        idx = None
        if step is not None:
            idx = next((k for k, s in enumerate(cap.steps) if s.id == step.id), None)
        return Failure(**self._base(), error_code=code, message=message, step_index=idx, step_id=step.id if step else None,
                       expected=expected, observed=self.redactor.text(observed), locator_attempts=attempts or [], evidence=evidence)

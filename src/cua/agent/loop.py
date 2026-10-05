"""Discovery: an LLM-driven observe -> decide -> act loop that ends in a recorded artifact.

Every proposed action goes through the PolicyGate before it runs. Each stop reason
is distinct and logged. The model only ever sees an "llm"-level redacted observation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlsplit

from cua.agent.llm import AgentDecision, LLMClient, LLMError
from cua.artifact import store
from cua.artifact.locators import build_candidates, propose
from cua.artifact.params import normalize, path_of
from cua.artifact.profile import AppProfile
from cua.artifact.recorder import ExtractSpec, FinishSpec, ProposedInput, RecordedAction, Recorder, RecorderError
from cua.artifact.schema import RiskClass, Sensitivity, TextVisible
from cua.evidence.log import RunLog, capture, new_run_id
from cua.handoff.coordinator import HandoffCoordinator
from cua.handoff.lock import ControlLock, GuardedSurface, SessionState
from cua.policy.gate import ActionContext, CredentialResolver, Decision, PolicyConfig, PolicyGate
from cua.policy.redaction import Redactor
from cua.replay.resolve import resolve_target
from cua.surface.types import ActionRequest, Observation, RefTarget, UIElement


class StopReason(StrEnum):
    GOAL_MET = "goal_met"
    GOAL_UNVERIFIED = "goal_unverified"
    MAX_STEPS = "max_steps"
    TIMEOUT = "timeout"
    NO_PROGRESS = "no_progress"
    POLICY_BLOCKED = "policy_blocked"
    HUMAN_ABORTED = "human_aborted"
    NEEDS_HUMAN = "needs_human"
    LLM_ERROR = "llm_error"
    RECORDER_ERROR = "recorder_error"


@dataclass
class DiscoveryOptions:
    max_steps: int | None = None
    timeout_s: int | None = None
    headed: bool = False
    escalation: Literal["detach", "wait"] = "wait"
    handoff_timeout_s: float = 300
    runs_root: Path = field(default_factory=lambda: Path("runs"))
    artifacts_root: Path = field(default_factory=lambda: Path("artifacts"))
    trace: bool = True


@dataclass
class DiscoveryResult:
    run_id: str
    run_dir: Path
    stop_reason: StopReason
    detail: str = ""
    artifact_path: Path | None = None
    capability_id: str | None = None
    version: str | None = None
    steps_taken: int = 0


class _Stop(Exception):
    def __init__(self, reason: StopReason, detail: str = "") -> None:
        self.reason, self.detail = reason, detail


class DiscoveryRunner:
    def __init__(self, llm: LLMClient, policy: PolicyConfig, profile: AppProfile, options: DiscoveryOptions | None = None) -> None:
        self.llm = llm
        self.policy = policy
        self.gate = PolicyGate(policy)
        self.profile = profile
        self.opt = options or DiscoveryOptions()
        self.max_steps = self.opt.max_steps or policy.discovery.max_steps
        self.timeout_s = self.opt.timeout_s or policy.discovery.timeout_s

    # ================================================================== entry point
    def run(self, goal: str, target: str) -> DiscoveryResult:
        self.run_id = new_run_id("discover")
        self.run_dir = self.opt.runs_root / self.run_id
        self.redactor = Redactor(self.policy.redaction)
        self.creds = CredentialResolver(self.policy, self.redactor)
        for ref in self.policy.credentials:
            try:
                self.creds.get(ref)
            except KeyError:
                pass
        self.log = RunLog(self.run_dir, self.run_id, self.redactor)
        parts = urlsplit(target)
        self.base_url = f"{parts.scheme}://{parts.netloc}"
        self.entry_point = parts.path if parts.path not in ("", "/") else "/app"
        self.goal = goal
        self.actions: list[RecordedAction] = []
        self.log.write_json("meta.json", {"kind": "discovery", "goal": goal, "target": target, "model": self.llm.model_name, "started_at": time.time()})
        self.log.event("discovery_started", goal=goal, target=target, model=self.llm.model_name,
                       limits={"max_steps": self.max_steps, "timeout_s": self.timeout_s})

        from cua.surface.browser import BrowserSession
        from cua.surface.playwright_web import PlaywrightWebSurface

        self.browser = BrowserSession(self.run_dir, headed=self.opt.headed, trace=self.opt.trace)
        page = self.browser.start()
        self.surface = PlaywrightWebSurface(page, mask_rules={"log": self.redactor.mask_rules("log"), "llm": self.redactor.mask_rules("llm")})
        self.lock = ControlLock(self.run_id, self.run_dir / "session.json")
        self.guard = GuardedSurface(self.surface, self.lock)
        self.coord = HandoffCoordinator(
            run_dir=self.run_dir, lock=self.lock, log=self.log, surface=self.surface, mode="discovery",
            cdp_endpoint=self.browser.cdp_endpoint,
            timeout_s=self.opt.handoff_timeout_s if self.opt.escalation == "wait" else 0,
            bring_to_front=page.bring_to_front,
        )
        self.browser.on_human_event(self.coord.on_human_event)
        result = DiscoveryResult(self.run_id, self.run_dir, StopReason.MAX_STEPS)
        steps = 0
        try:
            finish, steps = self._loop()
            result = self._record(finish, steps)
        except _Stop as s:
            result = DiscoveryResult(self.run_id, self.run_dir, s.reason, s.detail, steps_taken=len(self.actions))
            if s.reason != StopReason.GOAL_MET:
                self.log.event("evidence", **capture(self.surface, self.log, f"stop-{s.reason}"))
        finally:
            if self.lock.state == SessionState.RUNNING:
                self.lock.transition("complete" if result.stop_reason == StopReason.GOAL_MET else "fail", "automation")
            trace = self.browser.close()
            if trace:
                self.log.event("trace_saved", path=str(trace.relative_to(self.run_dir)), note="restricted: unredacted DOM, not for sharing")
        self.log.event("discovery_finished", stop_reason=result.stop_reason, detail=result.detail,
                       artifact=str(result.artifact_path) if result.artifact_path else None, steps=result.steps_taken)
        self.log.finalize()
        return result

    # ================================================================== the loop
    def _loop(self) -> tuple[FinishSpec, int]:
        self._login()
        self.guard.act(ActionRequest(kind="navigate", value=urljoin(self.base_url, self.entry_point)))
        self.llm.start(self.goal, urljoin(self.base_url, self.entry_point))
        deadline = time.monotonic() + self.timeout_s
        feedback: str | None = None
        last_digest, repeats, finish_failures, budget_warned = None, 0, 0, False
        for k in range(self.max_steps):
            if time.monotonic() > deadline:
                raise _Stop(StopReason.TIMEOUT, f"wall-clock limit {self.timeout_s}s")
            self.surface.settle()
            obs = self.surface.observe(screenshot=True, mask_level="llm")
            llm_obs = self.redactor.observation(obs, level="llm")
            llm_obs.screenshot = obs.screenshot
            digest = obs.digest()
            repeats = repeats + 1 if digest == last_digest else 0
            last_digest = digest
            self.log.event("observation", step=k, url=obs.url, frames=[f.url for f in obs.frames], elements=len(obs.elements), digest=digest)
            if repeats >= self.policy.discovery.no_progress_repeats:
                feedback = self._stuck(f"no progress: the screen did not change for {repeats} actions", StopReason.NO_PROGRESS)
                repeats = 0
                continue
            if not budget_warned and k >= int(self.max_steps * 0.8):
                budget_warned = True
                self.log.event("step_budget_warning", step=k, max_steps=self.max_steps)
                if self.opt.escalation == "wait":
                    feedback = self._stuck(f"step budget nearly exhausted ({k}/{self.max_steps})", StopReason.MAX_STEPS)
                    continue
            try:
                d = self.llm.decide(llm_obs, feedback)
            except LLMError as e:
                raise _Stop(StopReason.LLM_ERROR, str(e))
            if d.tool == "type" and d.args.get("text"):
                # unknown yet whether it's PII (inputs are classified at finish): mask in logs from now on
                self.redactor.register(str(d.args["text"]), "typed", partial=True, strict=False)
            self.log.event("llm_decision", step=k, tool=d.tool, args=d.args, reason=d.reason, usage=d.usage or None)
            if d.tool == "finish":
                ok, feedback = self._check_finish(d)
                if ok:
                    return self._finish_spec(d), k + 1
                finish_failures += 1
                if finish_failures >= 2:
                    raise _Stop(StopReason.GOAL_UNVERIFIED, feedback)
                continue
            feedback = self._dispatch(d, obs)
        raise _Stop(StopReason.MAX_STEPS, f"{self.max_steps} steps without finishing")

    def _stuck(self, reason: str, stop: StopReason) -> str:
        if self.opt.escalation != "wait":
            raise _Stop(stop, reason)
        outcome = self.coord.escalate(kind="stuck", reason=reason, goal=self.goal, step_index=len(self.actions))
        return self._after_handoff(outcome)

    def _after_handoff(self, outcome) -> str:
        if outcome.decision in ("detached", "timeout"):
            raise _Stop(StopReason.NEEDS_HUMAN, f"intervention {outcome.intervention_id} unanswered")
        if outcome.decision == "aborted":
            raise _Stop(StopReason.HUMAN_ABORTED, f"operator aborted ({outcome.intervention_id})")
        self.lock.transition("verified", "automation", "discovery re-observes after handoff")
        self.guard.refresh_token()
        if outcome.decision == "approved":
            return "approved"
        human = self._human_actions()
        self.actions += human
        summary = "; ".join(f"{a.kind} {a.element.name if a.element else a.value}" for a in human) or "nothing"
        return f"operator performed: {summary}. Re-observe and continue toward the goal."

    # ================================================================== dispatch
    def _dispatch(self, d: AgentDecision, obs: Observation) -> str:
        if d.tool in ("click", "type", "select", "extract"):
            el = obs.by_ref(str(d.args.get("ref", "")))
            if el is None:
                return f"error: unknown ref {d.args.get('ref')!r}; use a ref from the latest observation"
            if d.tool == "extract":
                return self._extract(d, el)
            return self._ui_action(d, el, obs)
        if d.tool == "navigate":
            return self._navigate(d)
        if d.tool == "wait_for":
            text = str(d.args.get("text", ""))
            ok = any(self.surface.check(TextVisible(text=text), {}) or self.surface.wait(250) for _ in range(60))
            return f"ok: '{text}' is visible" if ok else f"error: '{text}' did not appear within 15s"
        if d.tool == "request_human":
            outcome = self.coord.escalate(kind="stuck", reason=d.reason or "model requested a human", goal=self.goal,
                                          step_index=len(self.actions))
            return self._after_handoff(outcome)
        return f"error: unknown tool {d.tool}"

    def _policy(self, action: str, info: dict, approved: bool = False):
        ctx = ActionContext(mode="discovery", action=action, approved_by_human=approved, **info)
        dec = self.gate.evaluate(ctx)
        self.log.event("policy", action=action, decision=dec.decision, reason=dec.reason, risk=dec.risk, rule=dec.rule)
        if dec.decision == Decision.BLOCK:
            raise _Stop(StopReason.POLICY_BLOCKED, dec.reason)
        return dec

    def _ui_action(self, d: AgentDecision, el: UIElement, obs: Observation) -> str:
        target = RefTarget(ref=el.ref)
        info = self.surface.describe_target(target)
        pinfo = {"url": info.get("href") if info.get("role") == "link" else None, "frame_url": info.get("frame_url"),
                 "target_role": info.get("role"), "target_name": info.get("name")}
        dec = self._policy(d.tool, pinfo)
        typed = [a.value for a in self.actions if a.kind in ("type", "select") and a.value]
        candidates, report = build_candidates(self.surface, el, typed_values=typed)
        self.log.event("locator_candidates", ref=el.ref, kept=[c.strategy for c in candidates],
                       report=[r.model_dump(mode="json") for r in report])
        if not candidates:
            return "error: that element cannot be identified reliably (no unique locator); pick another way"
        if dec.decision == Decision.REQUIRES_APPROVAL:
            outcome = self.coord.escalate(
                kind="approval", reason=dec.reason, goal=self.goal, step_index=len(self.actions),
                step_description=d.reason, proposed_action={"action": d.tool, "target": el.name, "risk": dec.risk},
            )
            fb = self._after_handoff(outcome)
            if fb != "approved":
                return fb  # the human did it (or something else) themselves
            dec = self._policy(d.tool, pinfo, approved=True)
            # the page is unchanged (nobody acted); the element and its verified candidates are still valid
        value = d.args.get("text") if d.tool == "type" else d.args.get("option")
        pre_frames = {self._key(f.path): f.url for f in obs.frames}
        r = self.guard.act(ActionRequest(kind=d.tool, target=target, value=value))
        self.log.event("action", tool=d.tool, ok=r.ok, error=r.error, duration_ms=r.duration_ms, value=value)
        if not r.ok:
            return f"error: {r.error}"
        self.surface.settle()
        navigated, landmarks = self._changes(obs, pre_frames, el)
        self.actions.append(RecordedAction(
            kind=d.tool, element=el, candidates=candidates, value=value, reason=d.reason, risk=dec.risk,
            frame_path=el.frame_path, pre_frame_url=info.get("frame_url"), navigated=navigated, landmarks=landmarks,
        ))
        where = f"; {navigated[0][1]} loaded" if navigated else ""
        return f"ok: {d.tool} done{where}"

    def _navigate(self, d: AgentDecision) -> str:
        url = urljoin(self.base_url, str(d.args.get("path", "")))
        dec = self._policy("navigate", {"url": url, "frame_url": url})
        obs = self.surface.observe(screenshot=False, mask_level=None)
        pre_frames = {self._key(f.path): f.url for f in obs.frames}
        r = self.guard.act(ActionRequest(kind="navigate", value=url))
        if not r.ok:
            return f"error: {r.error}"
        self.surface.settle()
        navigated, landmarks = self._changes(obs, pre_frames, None)
        self.actions.append(RecordedAction(kind="navigate", element=None, candidates=[], value=url, reason=d.reason,
                                           risk=dec.risk, frame_path=[], pre_frame_url=None, navigated=navigated, landmarks=landmarks))
        return f"ok: navigated to {path_of(url)}"

    def _extract(self, d: AgentDecision, el: UIElement) -> str:
        info = self.surface.describe_target(RefTarget(ref=el.ref))
        self._policy("extract", {"frame_url": info.get("frame_url")})
        raw = self.surface.read_ref(el.ref)
        typ = str(d.args.get("type", "string"))
        how = "currency_to_decimal" if typ == "decimal" and any(ch in raw for ch in "$,") else ("none" if typ == "decimal" else "trim")
        try:
            value = normalize(raw, how, typ)
        except Exception:  # noqa: BLE001
            return f"error: could not read a {typ} from that element"
        sens = Sensitivity(d.args.get("sensitivity", "internal"))
        if self.redactor.field_of(el):  # policy field rules can only make it stricter
            sens = Sensitivity.PII
        if sens == Sensitivity.PII:
            self.redactor.register(str(value), d.args.get("name", "output"))
            self.redactor.register(raw.strip(), d.args.get("name", "output"))
        hint = d.args.get("row_key_column")
        candidates, report = build_candidates(self.surface, el, row_key_hint=hint, is_data=True)
        self.log.event("locator_candidates", ref=el.ref, kept=[c.strategy for c in candidates],
                       report=[r.model_dump(mode="json") for r in report])
        if not candidates:
            return "error: that value cannot be located reliably; try the table cell itself"
        name = str(d.args.get("name", "value"))
        self.actions.append(RecordedAction(
            kind="extract", element=el, candidates=candidates, value=None, reason=d.reason, risk=RiskClass.SAFE,
            frame_path=el.frame_path, pre_frame_url=info.get("frame_url"),
            extract=ExtractSpec(name=name, type=typ, description=str(d.args.get("description", "")), sensitivity=sens, raw=raw, normalize=how),
        ))
        self.log.event("extracted", output=name, value=str(value), normalize=how)
        return f"ok: extracted {name} = {value}"

    # ================================================================== helpers
    @staticmethod
    def _key(path) -> str:
        return "/".join(f.name or f.url_pattern or "?" for f in path or [])

    def _changes(self, pre: Observation, pre_frames: dict[str, str], el: UIElement | None):
        post = self.surface.observe(screenshot=False, mask_level=None)
        navigated = []
        for f in sorted(post.frames, key=lambda f: len(f.path)):
            before = pre_frames.get(self._key(f.path))
            if before is None or path_of(before) != path_of(f.url) or before != f.url:
                navigated.append((f.path, f.url))
        scope = [p for p, _ in navigated] or ([el.frame_path] if el else [])
        pre_names = {(self._key(e.frame_path), e.name) for e in pre.elements}
        landmarks = []
        for e in post.elements:
            if e.frame_path not in scope or not e.emphasis or not (3 <= len(e.name) <= 40):
                continue
            if (self._key(e.frame_path), e.name) in pre_names or self.redactor.field_of(e) or e.name.replace(",", "").replace(".", "").replace("$", "").isdigit():
                continue
            landmarks.append((e.frame_path, e.name))
        return navigated, landmarks[:3]

    def _login(self) -> None:
        spec = self.profile.session
        url = urljoin(self.base_url, spec.login_route)
        self._policy("navigate", {"url": url, "frame_url": url})
        self.guard.act(ActionRequest(kind="navigate", value=url))
        for step in spec.login_steps:
            res = resolve_target(self.surface, step.target, step.timeout_ms)
            if res.target is None:
                raise _Stop(StopReason.NEEDS_HUMAN, f"sign-on step {step.id}: target not found")
            value = self.creds.get(step.secret_ref) if step.secret_ref else step.value
            self.guard.act(ActionRequest(kind=step.action, target=res.target, value=value))
        self.surface.settle()
        if not all(self.surface.check(c, {}) for c in spec.logged_in):
            raise _Stop(StopReason.NEEDS_HUMAN, "sign-on did not reach the workstation")
        self.log.event("login", credential_refs=spec.credential_refs)

    def _human_actions(self) -> list[RecordedAction]:
        """Turn captured operator events into (unverified) recorded actions."""
        out: list[RecordedAction] = []
        frames = {f.name: f for f in self.surface.page.frames}
        for source, p in self.coord.human_raw:
            role, kind = p.get("role"), p.get("kind")
            if kind == "click" and role in ("link", "button"):
                action, value = "click", None
            elif kind == "change" and role == "textbox":
                action, value = "type", p.get("value")
            elif kind == "change" and role == "combobox":
                action, value = "select", p.get("value")
            else:
                continue
            fr = frames.get(source.get("frame_name"))
            path = self.surface.frame_path(fr) if fr else []
            el = UIElement(ref="", frame_path=path, role=role, name=p.get("name") or "", tag=p.get("tag") or "",
                           label=p.get("label"), near_label=p.get("near_label"), column=p.get("column"), row=p.get("row"),
                           css=p.get("css") or "")
            out.append(RecordedAction(kind=action, element=el, candidates=propose(el, [], None, False), value=value,
                                      reason="performed by operator", risk=RiskClass.REVERSIBLE, frame_path=path,
                                      pre_frame_url=source.get("frame_url"), performed_by="human", verified=False))
        self.coord.human_raw.clear()
        return out

    def _check_finish(self, d: AgentDecision) -> tuple[bool, str]:
        text = str(d.args.get("success_text", ""))
        if not text or not self.surface.check(TextVisible(text=text), {}):
            return False, f"error: success_text '{text}' is not visible; the goal is not verifiably met"
        if not self.actions:
            return False, "error: no actions were taken"
        return True, "ok"

    def _finish_spec(self, d: AgentDecision) -> FinishSpec:
        a = d.args
        inputs = [ProposedInput(name=i["name"], type=i["type"], observed_value=str(i["observed_value"]), description=i["description"],
                                sensitivity=i["sensitivity"], pattern=i.get("pattern"), enum_values=i.get("enum_values"))
                  for i in a.get("inputs", [])]
        return FinishSpec(capability_name=a["capability_name"], title=a["title"], summary=a["summary"],
                          success_text=a["success_text"], inputs=inputs)

    def _record(self, finish: FinishSpec, steps: int) -> DiscoveryResult:
        self.log.event("goal_met", success_text=finish.success_text, evidence=capture(self.surface, self.log, "goal-met"))
        for p in finish.inputs:
            if p.sensitivity == "pii":
                self.redactor.register(p.observed_value, p.name, partial=True)
        try:
            cap = Recorder(self.profile).build(
                goal_redacted=self.redactor.text(self.goal), run_id=self.run_id, model=self.llm.model_name,
                entry_point=self.entry_point, actions=self.actions, finish=finish, redactions_applied=list(self.redactor.applied),
            )
        except (RecorderError, ValueError) as e:
            raise _Stop(StopReason.RECORDER_ERROR, str(e))
        if leaks := self.redactor.find_leaks(cap.model_dump_json()):
            raise _Stop(StopReason.RECORDER_ERROR, f"artifact would persist sensitive values ({leaks}); declare them as inputs")
        cap.version, parent = store.next_version(self.opt.artifacts_root, cap)
        cap.provenance.parent_version = parent if parent != cap.version else None
        path = store.save(self.opt.artifacts_root, cap)
        self.log.event("artifact_saved", path=str(path), capability_id=cap.capability_id, version=cap.version,
                       fingerprint=cap.fingerprint, steps=[s.id for s in cap.steps])
        return DiscoveryResult(self.run_id, self.run_dir, StopReason.GOAL_MET, "goal verified", path, cap.capability_id, cap.version, len(self.actions))

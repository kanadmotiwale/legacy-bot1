"""Recorder: a successful discovery run -> a draft Capability artifact.

The run log is a transcript; the artifact is a contract. The recorder:
- turns each executed action into a Step with verified locator candidates,
  a precondition (where we acted) and a checkpoint (what changed),
- accepts LLM-proposed inputs only if their observed value literally appears in the
  flow, then replaces concrete values with parameters (`10042` -> `{member_id}`,
  `/member/10042` -> `/member/:member_id`),
- copies vendor-profile knowledge (sign-on, error handlers, idempotency probe),
- records provenance without any model transcript.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from cua.artifact.params import parameterize_route, parameterize_text, path_of, route_matches
from cua.artifact.profile import AppProfile
from cua.artifact.schema import (
    AppBinding,
    Capability,
    Constraints,
    FieldValue,
    FrameRef,
    Idempotency,
    InputParam,
    Locator,
    OutputField,
    Provenance,
    RiskClass,
    RouteMatches,
    Sensitivity,
    Step,
    Target,
    TextVisible,
)
from cua.surface.types import UIElement

RECORDER_VERSION = "0.1.0"


@dataclass
class ExtractSpec:
    name: str
    type: str
    description: str
    sensitivity: Sensitivity
    raw: str
    normalize: str


@dataclass
class RecordedAction:
    kind: str  # click | type | select | navigate | extract
    element: UIElement | None
    candidates: list[Locator]
    value: str | None
    reason: str
    risk: RiskClass
    frame_path: list[FrameRef] | None = None
    pre_frame_url: str | None = None
    navigated: list[tuple[list[FrameRef], str]] = field(default_factory=list)
    landmarks: list[tuple[list[FrameRef], str]] = field(default_factory=list)
    performed_by: str = "agent"
    verified: bool = True
    extract: ExtractSpec | None = None


@dataclass
class ProposedInput:
    name: str
    type: str
    observed_value: str
    description: str
    sensitivity: str
    pattern: str | None = None
    enum_values: list[str] | None = None


@dataclass
class FinishSpec:
    capability_name: str
    title: str
    summary: str
    success_text: str
    inputs: list[ProposedInput]


class RecorderError(Exception):
    pass


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return (s or "step")[:40]


def _describe(a: RecordedAction) -> str:
    e = a.element
    if a.kind == "navigate":
        return f"URL {path_of(a.value or '')}"
    if a.kind == "extract" and a.extract:
        return a.extract.name.replace("_", " ")
    if e is None:
        return a.kind
    name = e.label or (e.near_label or "").rstrip(":") or e.name or e.column or e.role
    return f"{name} {e.role}" if e.role in ("textbox", "combobox", "link", "button") else name


def _step_id(a: RecordedAction, used: set[str]) -> str:
    if a.kind == "extract" and a.extract:
        base = f"extract_{a.extract.name}"
    elif a.kind == "navigate":
        base = "navigate_" + _slug(path_of(a.value or "").replace("/", " "))
    else:
        e = a.element
        what = (e.label or (e.near_label or "").rstrip(":") or e.name or e.role) if e else a.kind
        base = f"{a.kind}_{_slug(what)}"
    base = _slug(base)
    sid, n = base, 2
    while sid in used:
        sid, n = f"{base}_{n}", n + 1
    used.add(sid)
    return sid


def _corpus(actions: list[RecordedAction]) -> str:
    bits: list[str] = []
    for a in actions:
        bits += [a.value or "", a.pre_frame_url or ""]
        bits += [u for _, u in a.navigated]
        bits += [t for _, t in a.landmarks]
        bits += [str(c.model_dump()) for c in a.candidates]
    return "\n".join(bits)


class Recorder:
    def __init__(self, profile: AppProfile, vendor_slug: str = "mockcore") -> None:
        self.profile = profile
        self.vendor_slug = vendor_slug

    def build(
        self,
        *,
        goal_redacted: str,
        run_id: str,
        model: str,
        entry_point: str,
        actions: list[RecordedAction],
        finish: FinishSpec,
        redactions_applied: list[str],
    ) -> Capability:
        if not actions:
            raise RecorderError("no actions recorded")
        notes: list[str] = []

        # ---- 1. contract: inputs (accepted only if their observed value appears in the flow)
        corpus = _corpus(actions) + "\n" + finish.success_text
        inputs: list[InputParam] = []
        observed: dict[str, str] = {}
        for p in finish.inputs:
            if not p.observed_value or p.observed_value not in corpus:
                notes.append(f"rejected proposed input '{p.name}': its value never appears in the recorded flow")
                continue
            sens = Sensitivity(p.sensitivity) if p.sensitivity in Sensitivity._value2member_map_ else Sensitivity.INTERNAL
            if sens == Sensitivity.SECRET:
                notes.append(f"rejected proposed input '{p.name}': secrets must be credential references")
                continue
            ptype = p.type if p.type in ("string", "integer", "decimal", "date", "enum") else "string"
            cons = Constraints(pattern=p.pattern, enum=p.enum_values) if (p.pattern or p.enum_values) else None
            if ptype == "enum" and not (cons and cons.enum):
                ptype = "string"
            inputs.append(InputParam(name=p.name, type=ptype, description=p.description, sensitivity=sens,
                                     constraints=cons, example=None if sens == Sensitivity.PII else p.observed_value))
            observed[p.name] = p.observed_value

        outputs: list[OutputField] = []
        for a in actions:
            if a.extract:
                x = a.extract
                outputs.append(OutputField(name=x.name, type=x.type, description=x.description,
                                           sensitivity=x.sensitivity, normalize=x.normalize))

        # ---- 2. steps
        used: set[str] = set()
        steps: list[Step] = []
        human_steps: list[str] = []
        for a in actions:
            sid = _step_id(a, used)
            target = None
            if a.kind != "navigate":
                if not a.candidates:
                    raise RecorderError(f"step {sid}: no locator candidate was unique at record time")
                target = Target(description=_describe(a), frame_path=a.frame_path, candidates=a.candidates)
            pre = []
            if a.pre_frame_url and a.frame_path is not None:
                pre = [RouteMatches(pattern=path_of(a.pre_frame_url), frame_path=a.frame_path)]
            checkpoint = []
            if a.kind in ("type", "select") and a.value is not None and target is not None:
                checkpoint = [FieldValue(target=target, equals=a.value)]
            elif a.kind in ("click", "navigate"):
                for fp, url in a.navigated[:1]:
                    checkpoint.append(RouteMatches(pattern=path_of(url), frame_path=fp))
                for fp, text in a.landmarks[:1]:
                    checkpoint.append(TextVisible(text=text, exact=True, frame_path=fp))
                if not checkpoint:
                    notes.append(f"step {sid}: nothing observable changed; no checkpoint recorded")
            value = a.value
            if a.kind == "navigate":
                value = path_of(a.value or "")
            steps.append(Step(
                id=sid,
                description=a.reason[:160] or _describe(a),
                action=a.kind,
                target=target,
                value=value if a.kind != "extract" else None,
                output=a.extract.name if a.extract else None,
                risk=a.risk,
                precondition=pre,
                checkpoint=checkpoint,
                performed_by="human" if a.performed_by == "human" else "agent",
            ))
            if a.performed_by == "human":
                human_steps.append(sid)
                if not a.verified:
                    notes.append(f"step {sid} was performed by an operator; its locators were not verified at record time - review before approval")

        # ---- 3. parameterize concrete values
        steps = [self._parameterize_step(s, observed) for s in steps]
        success_text, _ = parameterize_text(finish.success_text, observed)
        constants = [s.value for s in steps if s.action in ("type", "select") and s.value and "{" not in s.value]
        if constants:
            notes.append(f"{len(constants)} typed/selected value(s) remain constants (not parameterized)")

        # ---- 4. vendor profile knowledge
        routes_by_step: dict[str, list[str]] = {}
        for a, s in zip(actions, steps):
            # an error signature can only appear on a page the step itself produced
            routes_by_step[s.id] = [path_of(u) for _, u in a.navigated]
        handlers = []
        for ph in self.profile.handlers:
            if ph.routes == ["*"]:
                handlers.append(ph.to_handler("*"))
                continue
            applies = [sid for sid, rs in routes_by_step.items() if any(route_matches(r, u) for r in ph.routes for u in rs if u)]
            if applies:
                handlers.append(ph.to_handler(applies))
        used_outcomes = {h.response.outcome for h in handlers if h.response.type == "return_outcome"}
        outcomes = [o for o in self.profile.outcomes if o.name in used_outcomes]

        idempotency = None
        irreversible = [(a, s) for a, s in zip(actions, steps) if s.risk == RiskClass.IRREVERSIBLE]
        if irreversible:
            a, s = irreversible[0]
            probe = next((p for p in self.profile.idempotency_probes if route_matches(p.when_route, a.pre_frame_url or "")), None)
            if probe is None:
                raise RecorderError(f"step {s.id} is irreversible but the app profile has no idempotency probe for it")
            if missing := set(probe.key_inputs) - set(observed):
                raise RecorderError(f"idempotency probe needs inputs {sorted(missing)} which the capability does not declare")
            idempotency = Idempotency(key_inputs=probe.key_inputs, probe_steps=probe.probe_steps,
                                      exists=probe.exists, on_exists_extract=probe.on_exists_extract)
            for o in outputs:
                o.required = False
            notes.append("outputs are absent when already_existed=true (effect found by the idempotency probe)")

        cap = Capability(
            capability_id=".".join([self.vendor_slug] + [_slug(part) for part in finish.capability_name.split(".") if part.strip()]),
            version="1.0.0",
            title=finish.title,
            description=finish.summary,
            app=AppBinding(vendor_product=self.profile.vendor_product, app_version_range=self.profile.app_version_range,
                           surface=self.profile.surface, entry_point=entry_point, version_probe=self.profile.version_probe),
            inputs=inputs,
            outputs=outputs,
            outcomes=outcomes,
            session=self.profile.session,
            steps=steps,
            handlers=handlers,
            success=[TextVisible(text=success_text, exact=False, frame_path=None)],
            idempotency=idempotency,
            provenance=Provenance(discovery_run_id=run_id, model=model, recorded_at=datetime.now(timezone.utc),
                                  recorder_version=RECORDER_VERSION, goal_redacted=goal_redacted,
                                  redactions_applied=sorted(redactions_applied), human_steps=human_steps, notes=notes),
        )
        cap.fingerprint = cap.compute_fingerprint()
        return cap

    @staticmethod
    def _parameterize_step(step: Step, observed: dict[str, str]) -> Step:
        def walk(node: Any, key: str | None = None) -> Any:
            if isinstance(node, dict):
                return {k: walk(v, k) for k, v in node.items()}
            if isinstance(node, list):
                return [walk(v, key) for v in node]
            if isinstance(node, str) and key not in ("strategy", "kind", "role", "control_role", "action", "risk", "performed_by", "id"):
                if key in ("pattern", "url_pattern"):
                    return parameterize_route(node, observed)[0]
                return parameterize_text(node, observed)[0]
            return node

        data = walk(step.model_dump(mode="json"))
        if data["action"] == "navigate" and data.get("value"):
            path, _ = parameterize_route(data["value"], observed)
            data["value"] = re.sub(r":([a-z][a-z0-9_]*)", r"{\1}", path)
        return Step.model_validate(data)

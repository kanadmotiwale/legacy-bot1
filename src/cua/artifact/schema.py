"""Capability artifact schema.

An artifact is an agent-invocable capability with a contract, not a transcript:
- contract: typed inputs, typed outputs, declared business outcomes
- flow: ordered steps with multi-strategy targets, risk class, pre/post conditions
- exceptions: known error signatures -> category -> response
- binding: which vendor product / version / surface, plus per-tenant overrides
- provenance: where it came from (never the raw model transcript)

Templates: values and locator text use `{param}`; route patterns use `:param`.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0"
PARAM_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")
ROUTE_PARAM_RE = re.compile(r":([a-z][a-z0-9_]*)")
SEMVER_RE = r"^\d+\.\d+\.\d+$"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Sensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    PII = "pii"  # includes financial data about a person (balances)
    SECRET = "secret"  # credentials: referenced by name, never stored


class RiskClass(StrEnum):
    SAFE = "safe"  # read-only
    REVERSIBLE = "reversible"  # changes UI state only (typing, selecting, navigating)
    IRREVERSIBLE = "irreversible"  # commits a business transaction


class Status(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    DEPRECATED = "deprecated"


# --------------------------------------------------------------------------- targeting


class RoleName(Model):
    """Accessibility role + accessible name. Maps 1:1 onto UIA ControlType+Name / AX role+title."""

    strategy: Literal["role_name"] = "role_name"
    role: str
    name: str


class Label(Model):
    """Control programmatically associated with a label (<label for>, aria-label)."""

    strategy: Literal["label"] = "label"
    text: str


class NearLabel(Model):
    """Legacy forms: the control (or, with no control_role, the value cell) in the table
    cell right after the cell whose text is `label_text`."""

    strategy: Literal["near_label"] = "near_label"
    label_text: str
    control_role: str | None = None


class TextAnchor(Model):
    strategy: Literal["text"] = "text"
    text: str
    role: str | None = None


class TableCell(Model):
    """Structural anchor: the cell in column `column_header` of the row whose
    `row_key_column` cell equals `row_key_value`. If `control_role` is set, the
    control of that role inside the row (used for row action links)."""

    strategy: Literal["table_cell"] = "table_cell"
    row_key_column: str
    row_key_value: str
    column_header: str | None = None
    control_role: str | None = None


class Css(Model):
    """Last resort. Structural path without generated class names."""

    strategy: Literal["css"] = "css"
    selector: str


Locator = Annotated[
    RoleName | Label | NearLabel | TextAnchor | TableCell | Css, Field(discriminator="strategy")
]
STRATEGY_PRIORITY = ["role_name", "label", "near_label", "text", "table_cell", "css"]


class FrameRef(Model):
    name: str | None = None
    url_pattern: str | None = None


class Target(Model):
    description: str
    frame_path: list[FrameRef] | None = Field(default_factory=list, description="[] = top document; null = any frame")
    candidates: list[Locator] = Field(min_length=1, description="priority order; each verified unique at record time")


# --------------------------------------------------------------------------- conditions
# One condition language for checkpoints, preconditions, success and error detectors.
# frame_path None means "any frame".


class RouteMatches(Model):
    kind: Literal["route_matches"] = "route_matches"
    pattern: str
    frame_path: list[FrameRef] | None = None


class TextVisible(Model):
    kind: Literal["text_visible"] = "text_visible"
    text: str
    exact: bool = Field(False, description="exact element text (landmarks) vs substring (error signatures)")
    frame_path: list[FrameRef] | None = None


class ElementVisible(Model):
    kind: Literal["element_visible"] = "element_visible"
    target: Target


class FieldValue(Model):
    kind: Literal["field_value"] = "field_value"
    target: Target
    equals: str


class HttpStatus(Model):
    kind: Literal["http_status"] = "http_status"
    min: int
    max: int
    frame_path: list[FrameRef] | None = None


Condition = Annotated[
    RouteMatches | TextVisible | ElementVisible | FieldValue | HttpStatus, Field(discriminator="kind")
]


# --------------------------------------------------------------------------- contract


class Constraints(Model):
    pattern: str | None = None
    min_length: int | None = None
    max_length: int | None = None
    minimum: str | None = None  # decimal as string to avoid float drift
    maximum: str | None = None
    enum: list[str] | None = None


class InputParam(Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: Literal["string", "integer", "decimal", "date", "enum"]
    description: str
    required: bool = True
    constraints: Constraints | None = None
    sensitivity: Sensitivity
    example: str | None = Field(None, description="synthetic example; never a discovery value for PII")


class OutputField(Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: Literal["string", "decimal", "integer", "date", "boolean"]
    description: str
    sensitivity: Sensitivity
    normalize: Literal["none", "trim", "currency_to_decimal", "mdy_to_iso_date"] = "trim"
    required: bool = True


class OutcomeSpec(Model):
    """A business outcome the caller must handle - part of the signature, like a checked exception."""

    name: str
    description: str
    detail_fields: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- steps


class RetryPolicy(Model):
    max_attempts: int = Field(2, ge=1, le=5)
    backoff_ms: list[int] = Field(default_factory=lambda: [500, 2000])


class Step(Model):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$", description="stable id; overrides and logs key on it")
    description: str
    action: Literal["navigate", "click", "type", "select", "extract"]
    target: Target | None = None
    value: str | None = Field(None, description="template, e.g. '{member_id}' or a URL path")
    secret_ref: str | None = Field(None, description="credential reference to type; never a value")
    output: str | None = Field(None, description="for extract: name of the OutputField filled")
    risk: RiskClass
    precondition: list[Condition] = Field(default_factory=list)
    checkpoint: list[Condition] = Field(default_factory=list, description="all must hold after the step")
    timeout_ms: int = 10_000
    retry: RetryPolicy | None = None
    performed_by: Literal["agent", "human", "authored"] = "agent"

    @model_validator(mode="after")
    def _check(self) -> Step:
        if self.action in ("click", "type", "select", "extract") and self.target is None:
            raise ValueError(f"step {self.id}: {self.action} needs a target")
        if self.action == "navigate" and not self.value:
            raise ValueError(f"step {self.id}: navigate needs a value")
        if self.action == "extract" and not self.output:
            raise ValueError(f"step {self.id}: extract needs an output name")
        if self.risk == RiskClass.IRREVERSIBLE and self.retry is not None:
            raise ValueError(f"step {self.id}: irreversible steps must never auto-retry")
        if self.value and self.secret_ref:
            raise ValueError(f"step {self.id}: use value or secret_ref, not both")
        return self


# --------------------------------------------------------------------------- exception handlers


class DetailExtraction(Model):
    """Extract structured detail for a business outcome, e.g. per-field validation errors."""

    name: str
    anchor_text: str = Field(description="text heading a block of lines")
    parse: Literal["lines", "field_colon_message"] = "lines"
    frame_path: list[FrameRef] | None = None


class ReturnOutcome(Model):
    type: Literal["return_outcome"] = "return_outcome"
    outcome: str
    details: list[DetailExtraction] = Field(default_factory=list)


class Dismiss(Model):
    type: Literal["dismiss"] = "dismiss"
    target: Target
    max_times: int = 3


class Retry(Model):
    type: Literal["retry"] = "retry"
    policy: RetryPolicy = Field(default_factory=RetryPolicy)


class Reauthenticate(Model):
    type: Literal["reauthenticate"] = "reauthenticate"
    max_times: int = 1


class Escalate(Model):
    type: Literal["escalate"] = "escalate"
    reason: str


class Fail(Model):
    type: Literal["fail"] = "fail"
    error_code: str


HandlerResponse = Annotated[
    ReturnOutcome | Dismiss | Retry | Reauthenticate | Escalate | Fail, Field(discriminator="type")
]
_CATEGORY_RESPONSES = {
    "business_outcome": {"return_outcome"},
    "recoverable": {"dismiss", "retry", "reauthenticate"},
    "hard_failure": {"fail", "escalate"},
}


class Handler(Model):
    id: str
    description: str
    detect: list[Condition] = Field(min_length=1, description="all must hold")
    category: Literal["business_outcome", "recoverable", "hard_failure"]
    applies_to: list[str] | Literal["*"] = "*"
    response: HandlerResponse

    @model_validator(mode="after")
    def _category_matches_response(self) -> Handler:
        if self.response.type not in _CATEGORY_RESPONSES[self.category]:
            raise ValueError(f"handler {self.id}: {self.category} cannot respond with {self.response.type}")
        return self

    def applies(self, step_id: str | None) -> bool:
        return self.applies_to == "*" or (step_id is not None and step_id in self.applies_to)


# --------------------------------------------------------------------------- binding, session, idempotency


class TenantOverride(Model):
    base_url: str | None = None
    route_prefix: str | None = None
    text_map: dict[str, str] = Field(default_factory=dict, description="relabeled UI text, e.g. 'Member ID' -> 'Member #'")
    step_overrides: dict[str, list[Locator]] = Field(default_factory=dict, description="step_id -> candidates tried first")
    extra_handlers: list[Handler] = Field(default_factory=list)


class VersionProbe(Model):
    """Where the app reports its version, e.g. a footer 'MockCore Teller 4.3.1'."""

    frame_path: list[FrameRef] = Field(default_factory=list)
    pattern: str = Field(description="regex with one group capturing the version")


class AppBinding(Model):
    vendor_product: str
    app_version_range: str = Field(description="e.g. '>=4.0,<5.0'")
    surface: Literal["web", "legacy_web", "desktop"]
    entry_point: str = Field(description="path relative to the tenant base URL")
    version_probe: VersionProbe | None = None
    tenant_overrides: dict[str, TenantOverride] = Field(default_factory=dict)


class SessionSpec(Model):
    login_route: str
    login_steps: list[Step]
    logged_in: list[Condition]
    credential_refs: list[str]


class Idempotency(Model):
    key_inputs: list[str]
    probe_steps: list[Step] = Field(description="read-only steps that reveal whether the effect already exists")
    exists: list[Condition]
    on_exists_extract: list[Step] = Field(default_factory=list)


class Provenance(Model):
    discovery_run_id: str
    model: str
    recorded_at: datetime
    recorder_version: str
    goal_redacted: str
    redactions_applied: list[str] = Field(default_factory=list)
    human_steps: list[str] = Field(default_factory=list, description="step ids performed by an operator; need review")
    reviewed_by: str | None = None
    approved_at: datetime | None = None
    parent_version: str | None = None
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- the capability


class Capability(Model):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    capability_id: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    version: str = Field(pattern=SEMVER_RE)
    status: Status = Status.DRAFT
    title: str
    description: str
    app: AppBinding
    inputs: list[InputParam]
    outputs: list[OutputField]
    outcomes: list[OutcomeSpec] = Field(default_factory=list)
    session: SessionSpec | None = None
    steps: list[Step] = Field(min_length=1)
    handlers: list[Handler] = Field(default_factory=list)
    success: list[Condition] = Field(min_length=1)
    idempotency: Idempotency | None = None
    provenance: Provenance
    fingerprint: str = ""

    @model_validator(mode="after")
    def _consistency(self) -> Capability:
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")
        declared = {p.name for p in self.inputs}
        used = self.referenced_params()
        if missing := used - declared:
            raise ValueError(f"template references undeclared inputs: {sorted(missing)}")
        outputs = {o.name for o in self.outputs}
        extracted = {s.output for s in self.steps if s.action == "extract"}
        if missing := {o.name for o in self.outputs if o.required} - extracted:
            raise ValueError(f"required outputs never extracted: {sorted(missing)}")
        if stray := extracted - outputs:
            raise ValueError(f"extract steps fill undeclared outputs: {sorted(stray)}")
        outcome_names = {o.name for o in self.outcomes}
        for h in self.handlers:
            if isinstance(h.response, ReturnOutcome) and h.response.outcome not in outcome_names:
                raise ValueError(f"handler {h.id} returns undeclared outcome {h.response.outcome}")
            if h.applies_to != "*" and (bad := set(h.applies_to) - set(ids)):
                raise ValueError(f"handler {h.id} applies to unknown steps {sorted(bad)}")
        if any(s.risk == RiskClass.IRREVERSIBLE for s in self.steps) and self.idempotency is None:
            raise ValueError("capabilities with irreversible steps must declare an idempotency probe")
        for p in self.inputs:
            if p.sensitivity == Sensitivity.SECRET:
                raise ValueError(f"input {p.name}: secrets are credential references, not inputs")
        return self

    def referenced_params(self) -> set[str]:
        found: set[str] = set()

        def walk(node, key: str | None = None) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, k)
            elif isinstance(node, list):
                for v in node:
                    walk(v, key)
            elif isinstance(node, str):
                found.update(PARAM_RE.findall(node))
                if key in ("pattern", "url_pattern"):
                    found.update(ROUTE_PARAM_RE.findall(node))

        walk(self.model_dump(mode="json", include={"steps", "handlers", "success", "idempotency"}))
        return found

    def compute_fingerprint(self) -> str:
        """Hash of everything that affects behaviour or contract. Excludes status/provenance."""
        body = self.model_dump(
            mode="json",
            include={"capability_id", "app", "inputs", "outputs", "outcomes", "session", "steps", "handlers", "success", "idempotency"},
        )
        return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]

    def contract_signature(self) -> str:
        body = self.model_dump(mode="json", include={"inputs", "outputs", "outcomes"})
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]

    def step_index(self, step_id: str) -> int:
        return next(i for i, s in enumerate(self.steps) if s.id == step_id)

    def output(self, name: str) -> OutputField:
        return next(o for o in self.outputs if o.name == name)

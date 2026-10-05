"""The replay result contract: a discriminated union with exactly four cases.

- Success:          the capability did its job; outputs are typed and normalized
- BusinessOutcome:  a legitimate answer the caller must handle (e.g. member_not_found)
- NeedsHuman:       an intervention request was raised; includes its id and reason
- Failure:          hard failure with enough detail to debug (step, expected, observed, evidence)
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

ErrorCode = Literal[
    "invalid_input",
    "policy_blocked",
    "rate_limited",
    "target_not_found",
    "target_ambiguous",
    "action_failed",
    "checkpoint_failed",
    "extraction_failed",
    "app_error",
    "unrecognized_state",
    "reauth_failed",
    "idempotency_ambiguous",
    "aborted_by_operator",
    "app_version_mismatch",
    "internal_error",
]


class LocatorAttempt(BaseModel):
    strategy: str
    match_count: int | None = None
    error: str | None = None


class DriftSignal(BaseModel):
    step_id: str
    expected_strategy: str
    matched_strategy: str
    matched_index: int
    note: str = "a lower-priority locator matched; the UI may have drifted for this tenant/version"


class ReplayEvent(BaseModel):
    t_ms: int
    kind: str
    step_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class _Base(BaseModel):
    run_id: str
    capability_id: str
    version: str
    tenant: str | None = None
    duration_ms: int = 0
    events: list[ReplayEvent] = Field(default_factory=list)
    drift: list[DriftSignal] = Field(default_factory=list)
    evidence_dir: str = ""


class Success(_Base):
    status: Literal["success"] = "success"
    outputs: dict[str, Any]
    already_existed: bool = Field(False, description="idempotency probe found the effect already applied")


class BusinessOutcome(_Base):
    status: Literal["business_outcome"] = "business_outcome"
    outcome: str
    step_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class NeedsHuman(_Base):
    status: Literal["needs_human"] = "needs_human"
    intervention_id: str
    reason: str
    step_id: str | None = None


class Failure(_Base):
    status: Literal["failure"] = "failure"
    error_code: ErrorCode
    message: str
    step_index: int | None = None
    step_id: str | None = None
    expected: str | None = None
    observed: str | None = None
    locator_attempts: list[LocatorAttempt] = Field(default_factory=list)
    evidence: dict[str, str] = Field(default_factory=dict)


ReplayResult = Annotated[Success | BusinessOutcome | NeedsHuman | Failure, Field(discriminator="status")]
ReplayResultAdapter = TypeAdapter(ReplayResult)

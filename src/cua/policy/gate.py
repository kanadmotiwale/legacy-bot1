"""PolicyGate: the single choke point every action passes through, in discovery and replay."""

from __future__ import annotations

import json
import os
import re
import time
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field

from cua.artifact.params import route_matches
from cua.artifact.schema import RiskClass, Status
from cua.policy.redaction import RedactionConfig, Redactor


class IrreversibleRule(BaseModel):
    action: str | None = None
    route: str | None = None
    name_pattern: str | None = None


class DiscoveryPolicy(BaseModel):
    on_irreversible: Literal["requires_approval", "block"] = "requires_approval"
    on_block: Literal["stop"] = "stop"
    max_steps: int = 25
    timeout_s: int = 300
    no_progress_repeats: int = 3


class ReplayPolicy(BaseModel):
    irreversible_requires_approved_artifact: bool = True
    irreversible_requires_flag: bool = True
    allow_reauth: bool = True


class CapabilityLimits(BaseModel):
    max_runs_per_hour: int = 200
    allow_irreversible: bool = False


class PolicyConfig(BaseModel):
    allowed_origins: list[str]
    allowed_routes: list[str]
    allowed_actions: list[str]
    irreversible: list[IrreversibleRule] = Field(default_factory=list)
    discovery: DiscoveryPolicy = Field(default_factory=DiscoveryPolicy)
    replay: ReplayPolicy = Field(default_factory=ReplayPolicy)
    capabilities: dict[str, CapabilityLimits] = Field(default_factory=dict)
    credentials: dict[str, str] = Field(default_factory=dict)
    redaction: RedactionConfig = Field(default_factory=RedactionConfig)

    @classmethod
    def load(cls, path: str | Path) -> PolicyConfig:
        return cls.model_validate(yaml.safe_load(Path(path).read_text()))

    def limits(self, capability_id: str | None) -> CapabilityLimits:
        return self.capabilities.get(capability_id or "", self.capabilities.get("*", CapabilityLimits()))


class Decision(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"
    REQUIRES_APPROVAL = "requires_approval"


class ActionContext(BaseModel):
    mode: Literal["discovery", "replay"]
    action: str
    url: str | None = Field(None, description="navigation destination, or href of a clicked link")
    frame_url: str | None = Field(None, description="URL of the document being acted on")
    target_role: str | None = None
    target_name: str | None = None
    capability_id: str | None = None
    artifact_status: Status | None = None
    declared_risk: RiskClass | None = None
    allow_irreversible: bool = False
    approved_by_human: bool = False


class PolicyDecision(BaseModel):
    decision: Decision
    reason: str
    risk: RiskClass
    rule: str | None = None


class PolicyGate:
    def __init__(self, config: PolicyConfig) -> None:
        self.config = config

    # -------------------------------------------------------------- allowlist
    def url_allowed(self, url: str) -> tuple[bool, str]:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.config.allowed_origins:
            return False, f"origin {origin} not in allowlist"
        if not any(route_matches(p, url) for p in self.config.allowed_routes):
            return False, f"route {parts.path} not in allowlist"
        return True, "allowed"

    def classify(self, ctx: ActionContext) -> tuple[RiskClass, str | None]:
        if ctx.action in ("extract", "wait_for", "finish", "request_human"):
            return RiskClass.SAFE, None
        route = urlsplit(ctx.frame_url or "").path
        for i, r in enumerate(self.config.irreversible):
            if r.action and r.action != ctx.action:
                continue
            if r.route and not route_matches(r.route, route if route else "/"):
                continue
            if r.name_pattern and not re.search(r.name_pattern, ctx.target_name or ""):
                continue
            return RiskClass.IRREVERSIBLE, f"irreversible[{i}]"
        if ctx.declared_risk == RiskClass.IRREVERSIBLE:  # artifact may raise risk, never lower it
            return RiskClass.IRREVERSIBLE, "artifact"
        return RiskClass.REVERSIBLE, None

    def evaluate(self, ctx: ActionContext) -> PolicyDecision:
        risk, rule = self.classify(ctx)

        def d(decision: Decision, reason: str, r: str | None = rule) -> PolicyDecision:
            return PolicyDecision(decision=decision, reason=reason, risk=risk, rule=r)

        if ctx.action not in self.config.allowed_actions:
            return d(Decision.BLOCK, f"action '{ctx.action}' not allowed", "allowed_actions")
        for url in (ctx.url, ctx.frame_url):
            if url and not url.startswith(("about:", "data:")):
                ok, why = self.url_allowed(url)
                if not ok:
                    return d(Decision.BLOCK, why, "allowlist")
        if risk != RiskClass.IRREVERSIBLE:
            return d(Decision.ALLOW, "allowed")

        if ctx.mode == "discovery":
            if self.config.discovery.on_irreversible == "block":
                return d(Decision.BLOCK, "irreversible actions are blocked during discovery")
            if ctx.approved_by_human:
                return d(Decision.ALLOW, "irreversible action approved by operator")
            return d(Decision.REQUIRES_APPROVAL, "irreversible action needs operator approval")

        limits = self.config.limits(ctx.capability_id)
        if not limits.allow_irreversible:
            return d(Decision.BLOCK, f"capability {ctx.capability_id} may not perform irreversible actions")
        rp = self.config.replay
        if rp.irreversible_requires_approved_artifact and ctx.artifact_status != Status.APPROVED:
            return d(Decision.BLOCK, "irreversible step in an artifact that is not approved")
        if ctx.approved_by_human:
            return d(Decision.ALLOW, "irreversible action approved by operator")
        if rp.irreversible_requires_flag and not ctx.allow_irreversible:
            return d(Decision.REQUIRES_APPROVAL, "irreversible step without --allow-irreversible")
        return d(Decision.ALLOW, "irreversible step allowed: approved artifact + explicit flag")

    # -------------------------------------------------------------- rate limits
    def check_rate(self, capability_id: str, runs_root: Path) -> tuple[bool, str]:
        limit = self.config.limits(capability_id).max_runs_per_hour
        cutoff = time.time() - 3600
        n = 0
        for meta in runs_root.glob("*/meta.json"):
            try:
                m = json.loads(meta.read_text())
            except (OSError, ValueError):
                continue
            if m.get("capability_id") == capability_id and m.get("started_at", 0) >= cutoff:
                n += 1
        return (n < limit, f"{n} runs in the last hour (limit {limit})")


class CredentialResolver:
    """Resolves credential *references* to values at the moment of use. Values are
    registered with the redactor immediately and never returned to the LLM or logged."""

    def __init__(self, config: PolicyConfig, redactor: Redactor) -> None:
        self.mapping = config.credentials
        self.redactor = redactor

    def get(self, ref: str) -> str:
        env = self.mapping.get(ref)
        if not env:
            raise KeyError(f"unknown credential reference {ref}")
        value = os.environ.get(env)
        if not value:
            raise KeyError(f"credential {ref}: environment variable {env} is not set (see .env.example)")
        self.redactor.register_secret(value, ref)
        return value

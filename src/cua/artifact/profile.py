"""Vendor app profiles: product-level knowledge shared by every tenant running the product."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import Field

from cua.artifact.schema import (
    Condition,
    Handler,
    Model,
    OutcomeSpec,
    SessionSpec,
    Step,
    VersionProbe,
)


class ProfileHandler(Handler):
    routes: list[str] = Field(default_factory=lambda: ["*"], description="routes where this signature can occur")

    def to_handler(self, applies_to: list[str] | str) -> Handler:
        data = self.model_dump(exclude={"routes"})
        data["applies_to"] = applies_to
        return Handler.model_validate(data)


class IdempotencyProbe(Model):
    when_route: str
    key_inputs: list[str]
    probe_steps: list[Step]
    exists: list[Condition]
    on_exists_extract: list[Step] = Field(default_factory=list)


class AppProfile(Model):
    vendor_product: str
    app_version_range: str
    surface: str
    version_probe: VersionProbe | None = None
    session: SessionSpec
    outcomes: list[OutcomeSpec] = Field(default_factory=list)
    handlers: list[ProfileHandler] = Field(default_factory=list)
    idempotency_probes: list[IdempotencyProbe] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> AppProfile:
        return cls.model_validate(yaml.safe_load(Path(path).read_text()))

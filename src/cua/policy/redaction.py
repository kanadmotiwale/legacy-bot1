"""The single redaction module. Everything written to disk passes through a Redactor.

Three layers, cheapest-to-most-general:
1. Known values: secrets resolved at runtime, sensitive inputs, and sensitive values
   *observed* on screen (via field rules) are registered and scrubbed wherever they
   appear later (URLs, page text, error messages).
2. Field rules: UI fields identified by adjacent label or column header ("SSN:",
   "Balance") are masked in structured observations and in screenshots.
3. Patterns: SSN / card / email / phone regexes as a backstop.

Two levels: "log" (everything persisted) hides all sensitive fields; "llm" (what the
discovery model sees) hides only fields the task never needs (SSN, DOB, ...).
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from cua.surface.types import Observation, UIElement

Level = Literal["log", "llm"]


class RedactionConfig(BaseModel):
    sensitive_labels: dict[str, str] = Field(default_factory=dict, description="adjacent label text -> field name")
    sensitive_columns: dict[str, str] = Field(default_factory=dict, description="column header -> field name")
    llm_hidden_fields: list[str] = Field(default_factory=list)
    sensitive_keys: list[str] = Field(default_factory=lambda: ["password", "secret", "token", "api_key", "authorization", "cookie"])


PATTERNS: list[tuple[str, re.Pattern]] = [
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")),
    ("phone", re.compile(r"\(\d{3}\)\s?\d{3}-\d{4}")),
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
]


class Redactor:
    def __init__(self, config: RedactionConfig | None = None) -> None:
        self.config = config or RedactionConfig()
        self._values: dict[str, str] = {}  # concrete value -> replacement
        self._strict: dict[str, str] = {}  # values that must never be persisted anywhere (PII fields, secrets)
        self.applied: set[str] = set()

    # ------------------------------------------------------------- registration
    def register_secret(self, value: str | None, ref: str) -> None:
        if value:
            self._values[value] = f"[SECRET:{ref}]"
            self._strict[value] = f"secret:{ref}"
            self.applied.add(f"secret:{ref}")

    def register(self, value: str | None, field: str, partial: bool = False, strict: bool = True) -> None:
        """Scrub `value` from everything written from now on. `strict` values also must never
        appear in an artifact (see find_leaks); typed-but-unclassified values are not strict."""
        v = (value or "").strip()
        if len(v) < 3 or v.startswith("[REDACTED"):
            return
        if strict:
            self._strict[v] = field
        if v in self._values and not partial:
            return
        self._values[v] = f"•••{v[-2:]}" if partial and len(v) >= 5 else f"[REDACTED:{field}]"
        self.applied.add(f"field:{field}")

    def find_leaks(self, text: str) -> list[str]:
        """Fields whose sensitive values appear in `text` (used to refuse persisting an artifact)."""
        return sorted({f for v, f in self._strict.items() if v in text})

    # ------------------------------------------------------------- text
    def text(self, s: str) -> str:
        if not s:
            return s
        for value in sorted(self._values, key=len, reverse=True):
            if value in s:
                s = s.replace(value, self._values[value])
        for name, pat in PATTERNS:
            if pat.search(s):
                s = pat.sub(f"[REDACTED:{name}]", s)
                self.applied.add(f"pattern:{name}")
        return s

    def obj(self, o: Any) -> Any:
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, BaseModel):
            return self.obj(o.model_dump(mode="json"))
        if isinstance(o, dict):
            out = {}
            for k, v in o.items():
                if isinstance(k, str) and any(s in k.lower() for s in self.config.sensitive_keys) and v:
                    out[k] = "[REDACTED:key]"
                else:
                    out[k] = self.obj(v)
            return out
        if isinstance(o, (list, tuple)):
            return [self.obj(v) for v in o]
        return o

    # ------------------------------------------------------------- structured UI
    def field_of(self, e: UIElement) -> str | None:
        if e.near_label and e.near_label in self.config.sensitive_labels:
            return self.config.sensitive_labels[e.near_label]
        if e.column and e.column in self.config.sensitive_columns:
            return self.config.sensitive_columns[e.column]
        return None

    def observation(self, obs: Observation, level: Level = "log") -> Observation:
        """Return a copy with sensitive fields masked. Also learns observed sensitive values."""
        hidden = set(self.config.llm_hidden_fields) if level == "llm" else None
        out: list[UIElement] = []
        for e in obs.elements:
            e = e.model_copy(deep=True)
            field = self.field_of(e)
            if field:
                e.sensitive_field = field
                self.register(e.name, field)
                if e.value:
                    self.register(e.value, field)
            if e.row:
                for col, val in e.row.items():
                    if col in self.config.sensitive_columns:
                        self.register(val, self.config.sensitive_columns[col])
            if field and (hidden is None or field in hidden):
                e.name = f"[REDACTED:{field}]"
                if e.value:
                    e.value = f"[REDACTED:{field}]"
            if e.row:
                e.row = {
                    k: (
                        f"[REDACTED:{self.config.sensitive_columns[k]}]"
                        if k in self.config.sensitive_columns
                        and (hidden is None or self.config.sensitive_columns[k] in hidden)
                        else v
                    )
                    for k, v in e.row.items()
                }
            if level == "log":
                e.name = self.text(e.name)
                e.value = self.text(e.value) if e.value else e.value
                e.row = {k: self.text(v) for k, v in e.row.items()} if e.row else e.row
            out.append(e)
        frames = [f.model_copy(update={"url": self.text(f.url) if level == "log" else f.url}) for f in obs.frames]
        return Observation(url=self.text(obs.url) if level == "log" else obs.url, frames=frames, elements=out)

    def mask_rules(self, level: Level = "log") -> dict[str, list[str]]:
        hidden = set(self.config.llm_hidden_fields) if level == "llm" else None
        keep = lambda f: hidden is None or f in hidden  # noqa: E731
        return {
            "labels": [lab for lab, f in self.config.sensitive_labels.items() if keep(f)],
            "columns": [col for col, f in self.config.sensitive_columns.items() if keep(f)],
        }

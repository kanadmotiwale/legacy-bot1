"""Template rendering, route patterns, parameterization and input validation."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel

from cua.artifact.schema import PARAM_RE, ROUTE_PARAM_RE, InputParam

# ---------------------------------------------------------------- rendering


def render(template: str, params: dict[str, Any]) -> str:
    def sub(m: re.Match) -> str:
        if m.group(1) not in params:
            raise KeyError(f"missing parameter {m.group(1)}")
        return str(params[m.group(1)])

    return PARAM_RE.sub(sub, template)


def render_model(model: BaseModel, params: dict[str, Any], text_map: dict[str, str] | None = None) -> Any:
    """Return a copy of a pydantic model with every string field rendered.

    `text_map` applies per-tenant relabeling (exact-match string replacement) after rendering.
    """

    def walk(node):
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str):
            out = render(node, params) if PARAM_RE.search(node) else node
            return text_map.get(out, out) if text_map else out
        return node

    return type(model).model_validate(walk(model.model_dump(mode="json")))


def route_regex(pattern: str) -> re.Pattern:
    parts = []
    for chunk in re.split(r"(:[a-z][a-z0-9_]*|\*\*|\*)", pattern):
        if chunk == "**":
            parts.append(".*")
        elif chunk == "*":
            parts.append("[^/]*")
        elif ROUTE_PARAM_RE.fullmatch(chunk or ""):
            parts.append("[^/]+")
        else:
            parts.append(re.escape(chunk))
    return re.compile("^" + "".join(parts) + "$")


def path_of(url: str) -> str:
    return urlsplit(url).path or "/"


def route_matches(pattern: str, url: str, params: dict[str, Any] | None = None) -> bool:
    """Match a URL's path against a route pattern. With params, `:name` must equal the value."""
    if params:
        pattern = ROUTE_PARAM_RE.sub(lambda m: str(params.get(m.group(1), m.group(0))), pattern)
    return bool(route_regex(pattern).match(path_of(url)))


# ---------------------------------------------------------------- parameterization


def _ordered(observed: dict[str, str]) -> list[tuple[str, str]]:
    return sorted(((k, v) for k, v in observed.items() if v), key=lambda kv: -len(kv[1]))


def parameterize_text(text: str, observed: dict[str, str]) -> tuple[str, set[str]]:
    """Replace observed concrete values with `{name}` (longest first, token-bounded)."""
    used: set[str] = set()
    for name, value in _ordered(observed):
        pat = re.compile(r"(?<![\w])" + re.escape(value) + r"(?![\w])")
        if pat.search(text):
            text = pat.sub("{" + name + "}", text)
            used.add(name)
    return text, used


def parameterize_route(url_or_path: str, observed: dict[str, str]) -> tuple[str, set[str]]:
    """`/member/10042/accounts` -> `/member/:member_id/accounts` (path only)."""
    used: set[str] = set()
    segs = path_of(url_or_path).split("/")
    by_value = {v: k for k, v in observed.items() if v}
    for i, seg in enumerate(segs):
        if seg in by_value:
            used.add(by_value[seg])
            segs[i] = ":" + by_value[seg]
    return "/".join(segs) or "/", used


# ---------------------------------------------------------------- input validation


class InputError(BaseModel):
    name: str
    message: str


def validate_inputs(spec: list[InputParam], raw: dict[str, str]) -> tuple[dict[str, Any], list[InputError]]:
    """Validate caller params against the typed inputs before touching any UI."""
    out: dict[str, Any] = {}
    errors: list[InputError] = []
    known = {p.name for p in spec}
    for extra in sorted(set(raw) - known):
        errors.append(InputError(name=extra, message="unknown parameter"))
    for p in spec:
        if p.name not in raw or raw[p.name] in ("", None):
            if p.required:
                errors.append(InputError(name=p.name, message="required"))
            continue
        v = str(raw[p.name]).strip()
        c = p.constraints
        try:
            if p.type == "integer":
                if not re.fullmatch(r"-?\d+", v):
                    raise ValueError("must be an integer")
            elif p.type == "decimal":
                d = Decimal(v)
                if not d.is_finite():
                    raise ValueError("must be a finite decimal")
                if c and c.minimum is not None and d < Decimal(c.minimum):
                    raise ValueError(f"must be >= {c.minimum}")
                if c and c.maximum is not None and d > Decimal(c.maximum):
                    raise ValueError(f"must be <= {c.maximum}")
                v = f"{d:.2f}"
            elif p.type == "date":
                date.fromisoformat(v)
            elif p.type == "enum":
                if not c or not c.enum or v not in c.enum:
                    raise ValueError(f"must be one of {c.enum if c else []}")
            if c:
                if c.pattern and not re.fullmatch(c.pattern, v):
                    raise ValueError(f"must match {c.pattern}")
                if c.min_length is not None and len(v) < c.min_length:
                    raise ValueError(f"min length {c.min_length}")
                if c.max_length is not None and len(v) > c.max_length:
                    raise ValueError(f"max length {c.max_length}")
            out[p.name] = v
        except (ValueError, InvalidOperation) as e:
            errors.append(InputError(name=p.name, message=str(e) or "invalid value"))
    return out, errors


# ---------------------------------------------------------------- output normalization


def normalize(raw: str, how: str, type_: str) -> Any:
    s = (raw or "").strip()
    if how == "currency_to_decimal":
        cleaned = re.sub(r"[,$\s]", "", s)
        neg = cleaned.startswith("(") and cleaned.endswith(")")
        cleaned = cleaned.strip("()")
        d = Decimal(cleaned)
        return str(-d if neg else d)
    if how == "mdy_to_iso_date":
        m, d, y = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s).groups()
        return date(int(y), int(m), int(d)).isoformat()
    if type_ == "integer":
        return int(s)
    if type_ == "boolean":
        return s.lower() in ("yes", "true", "y", "1")
    return s

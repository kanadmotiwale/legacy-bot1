"""Bind a (tenant-neutral) artifact to one tenant and one invocation's parameters.

Order: tenant overrides (route prefix, extra candidates, extra handlers) are applied
to the template, then params and the tenant's text map are rendered in. The engine
only ever executes a bound artifact.
"""

from __future__ import annotations

from typing import Any

from cua.artifact.params import render_model
from cua.artifact.schema import Capability, TenantOverride


def _prefix_routes(node: Any, prefix: str) -> Any:
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in ("pattern", "url_pattern", "login_route", "entry_point") and isinstance(v, str) and v.startswith("/"):
                out[k] = prefix + v
            elif k == "value" and node.get("action") == "navigate" and isinstance(v, str) and v.startswith("/"):
                out[k] = prefix + v
            else:
                out[k] = _prefix_routes(v, prefix)
        return out
    if isinstance(node, list):
        return [_prefix_routes(v, prefix) for v in node]
    return node


def bind(cap: Capability, tenant: str | None, params: dict[str, Any]) -> tuple[Capability, TenantOverride]:
    override = cap.app.tenant_overrides.get(tenant, TenantOverride()) if tenant else TenantOverride()
    data = cap.model_dump(mode="json")
    if override.route_prefix:
        for key in ("steps", "handlers", "success", "session", "idempotency"):
            data[key] = _prefix_routes(data[key], override.route_prefix)
        data["app"]["entry_point"] = override.route_prefix + data["app"]["entry_point"]
    for step in data["steps"]:
        extra = override.step_overrides.get(step["id"])
        if extra and step.get("target"):
            step["target"]["candidates"] = [c.model_dump() for c in extra] + step["target"]["candidates"]
    data["handlers"] += [h.model_dump(mode="json") for h in override.extra_handlers]
    templ = Capability.model_validate(data)
    return render_model(templ, params, override.text_map or None), override

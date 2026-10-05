"""Switchable runtime conditions for the mock app.

Toggled through the admin endpoint (`POST /__admin/conditions`), which is a test
harness. The automation's policy allowlist excludes `/__admin/**`, so the agent
can never flip these itself.

Semantics:
- sticky conditions stay on until reset: not_found, validation, permission_denied, slow
- one-shot conditions fire on the next member-detail page load, then clear:
  interstitial, session_expiry, error500, unknown_modal
"""

from __future__ import annotations

from pydantic import BaseModel


class Conditions(BaseModel):
    not_found: bool = False
    validation: bool = False
    permission_denied: bool = False
    slow_ms: int = 0
    interstitial: bool = False
    session_expiry: bool = False
    error500: int = 0  # number of upcoming detail-page loads that return 500
    unknown_modal: bool = False


INJECT_PRESETS: dict[str, dict] = {
    "not_found": {"not_found": True},
    "validation": {"validation": True},
    "permission": {"permission_denied": True},
    "interstitial": {"interstitial": True},
    "session_expiry": {"session_expiry": True},
    "slow": {"slow_ms": 2500},
    "500": {"error500": 1},
    "500_persistent": {"error500": 99},
    "unknown_modal": {"unknown_modal": True},
}

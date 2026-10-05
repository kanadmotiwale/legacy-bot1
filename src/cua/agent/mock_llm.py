"""MockLLM: a scripted stand-in behind the same interface, so the whole system runs offline.

It is not a recording: each scripted intent describes the element semantically
(role / name / label / column / row) and is resolved against the *live* observation
to a ref, exactly like the real model picks refs. If an expected element is missing it
does what a careful model should: asks for a human.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from cua.agent.llm import AgentDecision
from cua.surface.types import Observation, UIElement


@dataclass
class Intent:
    tool: str
    match: dict[str, Any] = field(default_factory=dict)
    args: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


def _matches(e: UIElement, spec: dict[str, Any]) -> bool:
    for k, want in spec.items():
        if k == "row":
            if not e.row or any(e.row.get(c) != v for c, v in want.items()):
                return False
        elif k == "name_contains":
            if want.lower() not in (e.name or "").lower():
                return False
        elif getattr(e, k, None) != want:
            return False
    return True


def lookup_script(goal: str) -> list[Intent]:
    mid = re.search(r"\b(\d{5})\b", goal)
    member_id = mid.group(1) if mid else "10042"
    return [
        Intent("click", {"role": "link", "name": "Member Search"}, reason="Open member search from the menu bar"),
        Intent("type", {"role": "textbox", "near_label": "Member ID:"}, {"text": member_id}, "Enter the member ID to search for"),
        Intent("click", {"role": "button", "name": "Search"}, reason="Run the search"),
        Intent("click", {"role": "link", "name": "View", "row": {"Member ID": member_id}}, reason="Open the matching member's detail page"),
        Intent(
            "extract",
            {"column": "Balance", "row": {"Type": "Share Savings"}},
            {"name": "savings_balance", "type": "decimal", "sensitivity": "pii",
             "description": "Current balance of the member's primary share savings account", "row_key_column": "Type"},
            "Read the share savings balance from the accounts table",
        ),
        Intent(
            "finish",
            args={
                "capability_name": "member.read_savings_balance",
                "title": "Read member savings balance",
                "summary": "Looks up a member by member ID and returns the current balance of their share savings account.",
                "success_text": "Member Detail",
                "inputs": [{"name": "member_id", "type": "string", "observed_value": member_id,
                            "description": "5-digit member ID", "sensitivity": "pii", "pattern": r"^\d{5}$", "enum_values": None}],
            },
            reason="The balance has been read from the member detail page",
        ),
    ]


def open_subaccount_script(goal: str) -> list[Intent]:
    mid = re.search(r"\b(\d{5})\b", goal)
    member_id = mid.group(1) if mid else "10042"
    nick = re.search(r"['\"]([A-Za-z0-9 ]{1,20})['\"]", goal)
    nickname = nick.group(1) if nick else "Rainy Day"
    amt = re.search(r"\$?(\d+\.\d{2})", goal)
    amount = amt.group(1) if amt else "25.00"
    acct_type = next((t for t in ("Vacation Club", "Holiday Club", "Sub-Savings") if t.lower() in goal.lower()), "Sub-Savings")
    funding = (re.search(r"\b([SC]\d{2})\b", goal) or [None, "S01"])[1]
    return [
        Intent("click", {"role": "link", "name": "Member Search"}, reason="Open member search"),
        Intent("type", {"role": "textbox", "near_label": "Member ID:"}, {"text": member_id}, "Enter the member ID"),
        Intent("click", {"role": "button", "name": "Search"}, reason="Run the search"),
        Intent("click", {"role": "link", "name": "View", "row": {"Member ID": member_id}}, reason="Open the member"),
        Intent("click", {"role": "link", "name": "Open Sub-Account"}, reason="Start opening a sub-account"),
        Intent("select", {"role": "combobox", "near_label": "Account Type:"}, {"option": acct_type}, "Choose the account type"),
        Intent("type", {"role": "textbox", "label": "Nickname:"}, {"text": nickname}, "Enter the nickname"),
        Intent("type", {"role": "textbox", "near_label": "Initial Deposit:"}, {"text": amount}, "Enter the initial deposit"),
        Intent("select", {"role": "combobox", "near_label": "Funding Account:"}, {"option": funding}, "Choose the funding account"),
        Intent("click", {"role": "button", "name": "Continue"}, reason="Go to the review screen"),
        Intent("click", {"role": "button", "name": "Submit"}, reason="Submit the reviewed sub-account (irreversible)"),
        Intent(
            "extract",
            {"near_label": "Reference Number:"},
            {"name": "reference_number", "type": "string", "sensitivity": "internal",
             "description": "Confirmation reference number of the new sub-account", "row_key_column": None},
            "Read the confirmation reference number",
        ),
        Intent(
            "finish",
            args={
                "capability_name": "subaccount.open",
                "title": "Open a sub-account",
                "summary": "Opens a new sub-account for a member, funded from an existing account, and returns the confirmation reference.",
                "success_text": "Sub-Account Opened",
                "inputs": [
                    {"name": "member_id", "type": "string", "observed_value": member_id, "description": "5-digit member ID",
                     "sensitivity": "pii", "pattern": r"^\d{5}$", "enum_values": None},
                    {"name": "account_type", "type": "enum", "observed_value": acct_type, "description": "Sub-account product",
                     "sensitivity": "public", "pattern": None, "enum_values": ["Sub-Savings", "Vacation Club", "Holiday Club"]},
                    {"name": "nickname", "type": "string", "observed_value": nickname, "description": "Nickname; also the idempotency key",
                     "sensitivity": "internal", "pattern": r"^[A-Za-z0-9 ]{1,20}$", "enum_values": None},
                    {"name": "initial_deposit", "type": "decimal", "observed_value": amount, "description": "Opening deposit in USD",
                     "sensitivity": "internal", "pattern": None, "enum_values": None},
                    {"name": "funding_account", "type": "string", "observed_value": funding, "description": "Suffix of the funding account, e.g. S01",
                     "sensitivity": "internal", "pattern": r"^[A-Z]\d{2}$", "enum_values": None},
                ],
            },
            reason="The confirmation screen with a reference number is showing",
        ),
    ]


class MockLLM:
    model_name = "mock-llm (scripted)"

    def __init__(self, script: list[Intent] | None = None) -> None:
        self._script = script
        self._i = 0
        self._retries = 0

    def start(self, goal: str, target: str) -> None:
        if self._script is None:
            self._script = open_subaccount_script(goal) if re.search(r"sub-?account", goal, re.I) else lookup_script(goal)
        self._i = 0

    def decide(self, observation: Observation, feedback: str | None) -> AgentDecision:
        if feedback and feedback.startswith("error") and self._i > 0:
            if self._retries >= 1:
                return AgentDecision("request_human", {}, f"My last action failed twice ({feedback})")
            self._retries += 1
            self._i -= 1  # retry the same intent once
        elif feedback and feedback.startswith("operator performed"):
            pass  # the human did the step we proposed; move on
        else:
            self._retries = 0
        if self._i >= len(self._script):
            return AgentDecision("request_human", {}, "Script exhausted without reaching the goal")
        intent = self._script[self._i]
        self._i += 1
        args = dict(intent.args)
        if intent.match:
            el = next((e for e in observation.elements if _matches(e, intent.match)), None)
            if el is None:
                return AgentDecision("request_human", {}, f"Expected element not found: {intent.match}")
            args["ref"] = el.ref
        return AgentDecision(intent.tool, args, intent.reason)

"""The constrained, typed tool set the discovery model may use. Nothing else exists."""

from __future__ import annotations

REASON = {"type": "string", "description": "One short sentence: why this action, now."}


def _tool(name: str, description: str, props: dict) -> dict:
    props = {**props, "reason": REASON}
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {"type": "object", "properties": props, "required": list(props), "additionalProperties": False},
    }


TOOLS: list[dict] = [
    _tool("click", "Click an element by its [ref] from the latest observation.", {"ref": {"type": "string"}}),
    _tool("type", "Replace the text of a textbox identified by [ref].", {"ref": {"type": "string"}, "text": {"type": "string"}}),
    _tool("select", "Choose an option (by its visible text) in a combobox identified by [ref].",
          {"ref": {"type": "string"}, "option": {"type": "string"}}),
    _tool("navigate", "Load a path on the target application (same origin only).", {"path": {"type": "string"}}),
    _tool("wait_for", "Wait until some text is visible (max 15 s).", {"text": {"type": "string"}}),
    _tool(
        "extract",
        "Read a value the goal asks for from element [ref] and declare it as a typed output of the capability.",
        {
            "ref": {"type": "string"},
            "name": {"type": "string", "description": "snake_case output name, e.g. savings_balance"},
            "type": {"type": "string", "enum": ["string", "decimal", "integer", "date"]},
            "description": {"type": "string"},
            "sensitivity": {"type": "string", "enum": ["public", "internal", "pii"]},
            "row_key_column": {"type": ["string", "null"],
                               "description": "if the value is in a table: the column that identifies the row (e.g. 'Type')"},
        },
    ),
    _tool("request_human", "Ask a human operator to take over the live session (you are stuck or unsure).", {}),
    _tool(
        "finish",
        "Declare the goal achieved. Propose the capability's inputs: every concrete value you typed, selected or "
        "navigated with that a caller would vary per invocation.",
        {
            "capability_name": {"type": "string", "description": "dotted snake_case, e.g. member.read_savings_balance"},
            "title": {"type": "string"},
            "summary": {"type": "string", "description": "what the capability does, for a calling agent"},
            "success_text": {"type": "string", "description": "text visible right now that proves the goal is met"},
            "inputs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {"type": "string", "enum": ["string", "integer", "decimal", "date", "enum"]},
                        "observed_value": {"type": "string", "description": "the exact value used in this run"},
                        "description": {"type": "string"},
                        "sensitivity": {"type": "string", "enum": ["public", "internal", "pii"]},
                        "pattern": {"type": ["string", "null"], "description": "regex the value must match, if known"},
                        "enum_values": {"type": ["array", "null"], "items": {"type": "string"}},
                    },
                    "required": ["name", "type", "observed_value", "description", "sensitivity", "pattern", "enum_values"],
                    "additionalProperties": False,
                },
            },
        },
    ),
]
TOOL_NAMES = {t["name"] for t in TOOLS}

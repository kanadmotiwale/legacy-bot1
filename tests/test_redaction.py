"""Redaction unit tests. (The end-to-end "no planted secrets on disk" check is in test_e2e.py.)"""

from __future__ import annotations

from cua.policy.redaction import RedactionConfig, Redactor
from cua.surface.types import FrameInfo, Observation, UIElement

CFG = RedactionConfig(
    sensitive_labels={"SSN:": "ssn", "Name:": "full_name"},
    sensitive_columns={"Balance": "balance"},
    llm_hidden_fields=["ssn"],
)


def test_secrets_patterns_and_keys():
    r = Redactor(CFG)
    r.register_secret("Plant3d-S3cret!", "mockcore.password")
    out = r.obj({"msg": "typed Plant3d-S3cret! for 900-55-1234, mail a@b.com", "password": "x", "nested": ["(555) 010-4477"]})
    assert "Plant3d" not in str(out) and "900-55-1234" not in str(out) and "a@b.com" not in str(out)
    assert out["password"] == "[REDACTED:key]" and "555" not in out["nested"][0]


def test_partial_mask_for_identifiers():
    r = Redactor(CFG)
    r.register("10042", "member_id", partial=True)
    assert r.text("/member/10042/accounts") == "/member/•••42/accounts"


def test_observation_field_rules_and_levels():
    obs = Observation(url="u", frames=[FrameInfo(path=[], url="u")], elements=[
        UIElement(ref="a", frame_path=[], role="cell", name="900-55-1234", near_label="SSN:"),
        UIElement(ref="b", frame_path=[], role="cell", name="Dana Q. Whitfield", near_label="Name:"),
        UIElement(ref="c", frame_path=[], role="cell", name="$12,345.67", column="Balance", row={"Type": "S", "Balance": "$12,345.67"}),
    ])
    r = Redactor(CFG)
    llm = r.observation(obs, level="llm")
    assert llm.elements[0].name == "[REDACTED:ssn]"          # hidden even from the model
    assert llm.elements[1].name == "Dana Q. Whitfield"       # the model may see it ...
    log = r.observation(obs, level="log")
    assert "Dana" not in log.outline() and "12,345" not in log.outline()  # ... logs never do
    # and values learned from the screen are scrubbed from any later text
    assert "Dana" not in r.text("error near Dana Q. Whitfield")


def test_artifact_leak_detection():
    r = Redactor(CFG)
    r.register("Dana Q. Whitfield", "full_name")
    r.register("Rainy Day", "typed", partial=True, strict=False)
    assert r.find_leaks('{"value": "Dana Q. Whitfield"}') == ["full_name"]
    assert r.find_leaks('{"value": "Rainy Day"}') == []

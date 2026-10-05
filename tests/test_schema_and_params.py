"""Artifact schema validation, round-trip, versioning, parameterization and input validation."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from cua.artifact import store
from cua.artifact.params import (
    normalize,
    parameterize_route,
    parameterize_text,
    render,
    route_matches,
    validate_inputs,
)
from cua.artifact.schema import (
    AppBinding,
    Capability,
    Handler,
    InputParam,
    OutputField,
    Provenance,
    RetryPolicy,
    RoleName,
    Step,
    Target,
    TableCell,
    TextVisible,
)


def make_cap(**over) -> Capability:
    target = Target(description="balance", frame_path=[], candidates=[
        TableCell(row_key_column="Member ID", row_key_value="{member_id}", column_header="Balance")])
    data = dict(
        capability_id="mockcore.member.read_balance",
        version="1.0.0",
        title="t",
        description="d",
        app=AppBinding(vendor_product="MockCore", app_version_range=">=4.0,<5.0", surface="legacy_web", entry_point="/app"),
        inputs=[InputParam(name="member_id", type="string", description="id", sensitivity="pii")],
        outputs=[OutputField(name="balance", type="decimal", description="b", sensitivity="pii", normalize="currency_to_decimal")],
        steps=[
            Step(id="open", description="open", action="navigate", value="/member/{member_id}", risk="safe",
                 checkpoint=[TextVisible(text="Member Detail", exact=True)]),
            Step(id="read", description="read", action="extract", target=target, output="balance", risk="safe"),
        ],
        success=[TextVisible(text="Member Detail")],
        provenance=Provenance(discovery_run_id="r", model="m", recorded_at=datetime.now(timezone.utc),
                              recorder_version="0", goal_redacted="g"),
    )
    data.update(over)
    return Capability(**data)


def test_valid_capability_round_trips():
    cap = make_cap()
    cap.fingerprint = cap.compute_fingerprint()
    again = Capability.model_validate_json(cap.model_dump_json())
    assert again == cap
    assert again.compute_fingerprint() == cap.fingerprint
    assert cap.referenced_params() == {"member_id"}


def test_undeclared_parameter_rejected():
    with pytest.raises(ValidationError, match="undeclared inputs"):
        make_cap(inputs=[])


def test_required_output_must_be_extracted():
    with pytest.raises(ValidationError, match="never extracted"):
        make_cap(outputs=[OutputField(name="other", type="string", description="x", sensitivity="public")])


def test_irreversible_step_rules():
    with pytest.raises(ValidationError, match="never auto-retry"):
        Step(id="submit", description="s", action="click", risk="irreversible", retry=RetryPolicy(),
             target=Target(description="b", candidates=[RoleName(role="button", name="Submit")]))
    submit = Step(id="submit", description="s", action="click", risk="irreversible",
                  target=Target(description="b", candidates=[RoleName(role="button", name="Submit")]))
    cap = make_cap()
    with pytest.raises(ValidationError, match="idempotency"):
        make_cap(steps=cap.steps + [submit])


def test_secret_inputs_rejected():
    with pytest.raises(ValidationError, match="credential references"):
        make_cap(inputs=[InputParam(name="member_id", type="string", description="x", sensitivity="pii"),
                         InputParam(name="pin", type="string", description="x", sensitivity="secret")])


def test_handler_category_must_match_response():
    with pytest.raises(ValidationError, match="cannot respond"):
        Handler(id="h", description="d", detect=[TextVisible(text="x")], category="business_outcome",
                response={"type": "retry"})


def test_versioning(tmp_path):
    cap = make_cap()
    cap.fingerprint = cap.compute_fingerprint()
    assert store.next_version(tmp_path, cap) == ("1.0.0", None)
    store.save(tmp_path, cap)
    assert store.next_version(tmp_path, cap) == ("1.0.0", "1.0.0")  # unchanged -> same version
    minor = make_cap(description="d", title="t")
    minor.steps[0].timeout_ms = 20_000
    minor.fingerprint = minor.compute_fingerprint()
    assert store.next_version(tmp_path, minor)[0] == "1.1.0"  # behaviour change, same contract
    major = make_cap(outputs=[OutputField(name="balance", type="string", description="b", sensitivity="pii")])
    major.fingerprint = major.compute_fingerprint()
    assert store.next_version(tmp_path, major)[0] == "2.0.0"  # contract change


def test_approved_artifacts_are_immutable(tmp_path):
    cap = make_cap()
    path = store.save(tmp_path, cap)
    store.approve(path, "alice")
    changed = make_cap()
    changed.steps[0].timeout_ms = 1234
    with pytest.raises(PermissionError):
        store.save(tmp_path, changed)


def test_json_schema_export(tmp_path):
    out = tmp_path / "capability.schema.json"
    store.export_schema(out)
    schema = json.loads(out.read_text())
    assert schema["title"] == "Capability" and "steps" in schema["properties"]


# ------------------------------------------------------------ parameterization


def test_parameterize_values_and_routes():
    observed = {"member_id": "10042"}
    assert parameterize_text("10042", observed) == ("{member_id}", {"member_id"})
    assert parameterize_text("100420", observed)[0] == "100420"  # token-bounded, no partial replace
    assert parameterize_route("http://h/member/10042/accounts?x=1", observed)[0] == "/member/:member_id/accounts"
    assert render("/member/{member_id}", {"member_id": "777"}) == "/member/777"
    assert route_matches("/member/:member_id", "http://h/member/777?ack=1")
    assert route_matches("/member/:member_id", "http://h/member/777", {"member_id": "777"})
    assert not route_matches("/member/:member_id", "http://h/member/778", {"member_id": "777"})
    assert route_matches("/tb/**", "http://h/tb/member/1/accounts")
    assert not route_matches("/member/*", "http://h/member/1/accounts")


def test_input_validation_before_ui():
    spec = [
        InputParam(name="member_id", type="string", description="", sensitivity="pii", constraints={"pattern": r"^\d{5}$"}),
        InputParam(name="amount", type="decimal", description="", sensitivity="internal", constraints={"minimum": "5"}),
        InputParam(name="kind", type="enum", description="", sensitivity="public", constraints={"enum": ["A", "B"]}),
    ]
    ok, errs = validate_inputs(spec, {"member_id": "10042", "amount": "25", "kind": "A"})
    assert not errs and ok["amount"] == "25.00"
    _, errs = validate_inputs(spec, {"member_id": "abc", "amount": "1", "kind": "C", "extra": "x"})
    assert {e.name for e in errs} == {"member_id", "amount", "kind", "extra"}


def test_output_normalization():
    assert normalize("$12,345.67", "currency_to_decimal", "decimal") == "12345.67"
    assert normalize("($5.00)", "currency_to_decimal", "decimal") == "-5.00"
    assert normalize("03/14/1984", "mdy_to_iso_date", "date") == "1984-03-14"

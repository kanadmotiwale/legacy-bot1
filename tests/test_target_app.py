"""Smoke tests for the mock bank app (HTTP level, no browser)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from target_app.app import app, credentials


@pytest.fixture()
def client():
    c = TestClient(app)
    c.post("/__admin/reset")
    user, pwd = credentials()
    r = c.post("/login", data={"usr": user, "pwd": pwd}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app"
    return c


def inject(c, **kw):
    c.post("/__admin/conditions", json=kw)


def test_login_required():
    c = TestClient(app)
    r = c.get("/member/search", follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]


def test_bad_password():
    c = TestClient(app)
    r = c.post("/login", data={"usr": "teller01", "pwd": "nope"}, follow_redirects=False)
    assert "failed=1" in r.headers["location"]


def test_lookup_flow(client):
    assert "<frameset" in client.get("/app").text
    r = client.get("/member/search", params={"fld1": "10042"})
    assert "Dana Q. Whitfield" in r.text and 'href="/member/10042"' in r.text
    assert "Member Detail" in client.get("/member/10042").text
    acct = client.get("/member/10042/accounts").text
    assert "Share Savings" in acct and "$12,345.67" in acct


def test_not_found_real_and_injected(client):
    assert "No member found" in client.get("/member/search", params={"fld1": "99999"}).text
    inject(client, not_found=True)
    assert "No member found" in client.get("/member/search", params={"fld1": "10042"}).text


def test_permission_denied(client):
    assert client.get("/member/10099").status_code == 403
    inject(client, permission_denied=True)
    r = client.get("/member/10042")
    assert r.status_code == 403 and "Access Denied" in r.text


def test_one_shot_conditions(client):
    inject(client, interstitial=True)
    assert "System Notice" in client.get("/member/10042").text
    assert "System Notice" not in client.get("/member/10042").text
    inject(client, unknown_modal=True)
    assert "Supervisor Override Required" in client.get("/member/10042").text
    assert "Member Detail" in client.get("/member/10042", params={"ack": 1}).text
    inject(client, error500=1)
    assert client.get("/member/10042").status_code == 500
    assert client.get("/member/10042").status_code == 200
    inject(client, session_expiry=True)
    r = client.get("/member/10042", follow_redirects=False)
    assert "/login?expired=1" in r.headers["location"]
    assert "/login" in client.get("/member/search", follow_redirects=False).headers["location"]


def test_open_subaccount_and_validation(client):
    form = {"m": "10042", "f_t": "Sub-Savings", "f_n": "Rainy Day", "f_d": "2.00", "f_f": "S01"}
    assert "Must be at least $5.00" in client.post("/subaccount/review", data=form).text
    form["f_d"] = "25.00"
    assert "Review Sub-Account" in client.post("/subaccount/review", data=form).text
    r = client.post("/subaccount/submit", data={**form, "act": "Submit"})
    assert "Reference Number" in r.text and "SA-" in r.text
    assert "Rainy Day" in client.get("/member/10042/accounts").text
    inject(client, validation=True)
    assert "daily transfer limit" in client.post("/subaccount/review", data=form).text


def test_tenant_b_variant(client):
    user, pwd = credentials()
    client.post("/tb/login", data={"usr": user, "pwd": pwd})
    r = client.get("/tb/member/search", params={"fld1": "10042"})
    assert "Member #" in r.text and "Risk Tier" in r.text and 'value="Find"' in r.text
    assert "Current Bal." in client.get("/tb/member/10042/accounts").text

"""MockCore Teller: a deliberately hostile "legacy" credit-union back-office app.

Hostility is intentional and mirrors real servicing tools:
- server-rendered HTML, frameset + nested iframe, table layouts
- header rows made of <td><b>, not <th>; page titles in <font>, not headings
- no data-testid, almost no ids, class names regenerated on every server start
- most form labels are adjacent table cells, not <label for>

Two tenants run the same vendor product: tenant "a" at `/`, and tenant "b" at
`/tb` with different branding, relabeled fields, an extra column and a newer
version. All data is synthetic.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from target_app.conditions import INJECT_PRESETS, Conditions
from target_app.data import Account, Store

TEMPLATES = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html"]),
)
_SALT = secrets.token_hex(4)


def cls(name: str) -> str:
    """Unstable, generated-looking class names: change every server start."""
    return "x" + hashlib.sha1(f"{_SALT}:{name}".encode()).hexdigest()[:6]


@dataclass
class Tenant:
    key: str
    prefix: str
    brand: str
    color: str
    version: str
    labels: dict[str, str] = field(default_factory=dict)
    extra_column: str | None = None

    def label(self, key: str) -> str:
        return self.labels.get(key, DEFAULT_LABELS[key])


DEFAULT_LABELS = {
    "member_id": "Member ID",
    "search_button": "Search",
    "search_title": "Member Search",
    "detail_title": "Member Detail",
    "balance": "Balance",
    "open_sub": "Open Sub-Account",
}

TENANTS = {
    "a": Tenant("a", "", "Northfield Community Credit Union", "#003366", "4.3.1"),
    "b": Tenant(
        "b",
        "/tb",
        "Lakeshore Federal Credit Union",
        "#5a1e00",
        "4.4.0",
        labels={
            "member_id": "Member #",
            "search_button": "Find",
            "detail_title": "Member Profile",
            "balance": "Current Bal.",
        },
        extra_column="Risk Tier",
    ),
}


def credentials() -> tuple[str, str]:
    return (
        os.environ.get("MOCKBANK_USER", "teller01"),
        os.environ.get("MOCKBANK_PASSWORD", "Plant3d-S3cret!"),
    )


class AppState:
    def __init__(self) -> None:
        self.store = Store()
        self.conditions = Conditions()
        self.sessions: dict[str, str] = {}  # token -> tenant key

    def reset(self) -> None:
        self.store.reset()
        self.conditions = Conditions()
        self.sessions.clear()


state = AppState()


def money(v: Decimal) -> str:
    return f"${v:,.2f}"


def render(name: str, tenant: Tenant, status: int = 200, **ctx) -> HTMLResponse:
    html = TEMPLATES.get_template(name).render(t=tenant, cls=cls, money=money, **ctx)
    return HTMLResponse(html, status_code=status)


def make_router(tenant: Tenant) -> APIRouter:
    r = APIRouter(prefix=tenant.prefix)
    p = tenant.prefix
    cookie = f"MCSESS_{tenant.key}"

    def authed(request: Request) -> bool:
        tok = request.cookies.get(cookie)
        return bool(tok) and state.sessions.get(tok) == tenant.key

    def to_login(request: Request) -> RedirectResponse:
        expired = "?expired=1" if request.cookies.get(cookie) else ""
        return RedirectResponse(f"{p}/login{expired}", status_code=303)

    @r.get("/", response_class=HTMLResponse)
    def root(request: Request):
        return RedirectResponse(f"{p}/app" if authed(request) else f"{p}/login", status_code=303)

    @r.get("/login", response_class=HTMLResponse)
    def login_page(expired: int = 0, failed: int = 0):
        return render("login.html", tenant, expired=expired, failed=failed)

    @r.post("/login")
    def login(usr: str = Form(""), pwd: str = Form("")):
        user, password = credentials()
        if not (secrets.compare_digest(usr, user) and secrets.compare_digest(pwd, password)):
            return RedirectResponse(f"{p}/login?failed=1", status_code=303)
        tok = secrets.token_hex(16)
        state.sessions[tok] = tenant.key
        resp = RedirectResponse(f"{p}/app", status_code=303)
        resp.set_cookie(cookie, tok, httponly=True)
        return resp

    @r.get("/logout")
    def logout(request: Request):
        state.sessions.pop(request.cookies.get(cookie, ""), None)
        resp = RedirectResponse(f"{p}/login", status_code=303)
        resp.delete_cookie(cookie)
        return resp

    @r.get("/app", response_class=HTMLResponse)
    def frameset(request: Request):
        if not authed(request):
            return to_login(request)
        return render("frameset.html", tenant)

    @r.get("/app/nav", response_class=HTMLResponse)
    def nav(request: Request):
        if not authed(request):
            return to_login(request)
        return render("nav.html", tenant)

    @r.get("/app/home", response_class=HTMLResponse)
    def home(request: Request):
        if not authed(request):
            return to_login(request)
        return render("home.html", tenant)

    @r.get("/member/search", response_class=HTMLResponse)
    def search(request: Request, fld1: str | None = None):
        if not authed(request):
            return to_login(request)
        results, error = None, None
        if fld1 is not None:
            q = fld1.strip()
            if not re.fullmatch(r"\d{5}", q):
                error = "Invalid member ID format. Enter a 5-digit member ID."
            else:
                m = state.store.members.get(q)
                results = [] if (m is None or state.conditions.not_found) else [m]
        return render("search.html", tenant, query=fld1 or "", results=results, error=error)

    @r.get("/member/{mid}", response_class=HTMLResponse)
    def detail(request: Request, mid: str, ack: int = 0):
        if not authed(request):
            return to_login(request)
        c = state.conditions
        if c.session_expiry:
            c.session_expiry = False
            state.sessions.pop(request.cookies.get(cookie, ""), None)
            return to_login(request)
        if c.error500 > 0:
            c.error500 -= 1
            return render("error500.html", tenant, status=500)
        m = state.store.members.get(mid)
        if m is None:
            return render("search.html", tenant, query=mid, results=[], error=None)
        if m.restricted or c.permission_denied:
            return render("denied.html", tenant, status=403)
        if c.unknown_modal and not ack:
            # An app state no handler knows about: a blocking page that needs a human decision.
            c.unknown_modal = False
            return render("override.html", tenant, m=m)
        modal = None
        if c.interstitial:
            c.interstitial = False
            modal = "interstitial"
        return render("detail.html", tenant, m=m, modal=modal)

    @r.get("/member/{mid}/accounts", response_class=HTMLResponse)
    def accounts(request: Request, mid: str):
        if not authed(request):
            return to_login(request)
        m = state.store.members.get(mid)
        if m is None or m.restricted:
            return render("denied.html", tenant, status=403)
        return render("accounts.html", tenant, m=m)

    @r.get("/subaccount/new", response_class=HTMLResponse)
    def sub_form(request: Request, m: str):
        if not authed(request):
            return to_login(request)
        member = state.store.members.get(m)
        if member is None:
            return render("denied.html", tenant, status=403)
        return render("sub_form.html", tenant, m=member, errors=[], v={})

    @r.post("/subaccount/review", response_class=HTMLResponse)
    def sub_review(
        request: Request,
        m: str = Form(...),
        f_t: str = Form(""),
        f_n: str = Form(""),
        f_d: str = Form(""),
        f_f: str = Form(""),
    ):
        if not authed(request):
            return to_login(request)
        member = state.store.members.get(m)
        if member is None:
            return render("denied.html", tenant, status=403)
        v = {"f_t": f_t, "f_n": f_n.strip(), "f_d": f_d.strip(), "f_f": f_f}
        errors = validate_sub(member, v)
        if errors:
            return render("sub_form.html", tenant, m=member, errors=errors, v=v)
        return render("sub_review.html", tenant, m=member, v=v, amount=money(Decimal(v["f_d"])))

    @r.post("/subaccount/submit", response_class=HTMLResponse)
    def sub_submit(
        request: Request,
        m: str = Form(...),
        f_t: str = Form(...),
        f_n: str = Form(...),
        f_d: str = Form(...),
        f_f: str = Form(...),
        act: str = Form("Submit"),
    ):
        if not authed(request):
            return to_login(request)
        member = state.store.members.get(m)
        if member is None:
            return render("denied.html", tenant, status=403)
        v = {"f_t": f_t, "f_n": f_n, "f_d": f_d, "f_f": f_f}
        if act != "Submit":
            return render("sub_form.html", tenant, m=member, errors=[], v=v)
        errors = validate_sub(member, v)
        if errors:
            return render("sub_form.html", tenant, m=member, errors=errors, v=v)
        # Deliberately NOT idempotent, like most legacy apps: a double submit opens two accounts.
        amount = Decimal(f_d)
        funding = next(a for a in member.accounts if a.suffix == f_f)
        funding.balance -= amount
        suffix = f"S{len(member.accounts) + 1:02d}"
        member.accounts.append(Account(suffix, f_t, f_n, amount, "10/05/2026"))
        ref = state.store.next_reference()
        return render("sub_confirm.html", tenant, m=member, ref=ref, suffix=suffix, nickname=f_n)

    return r


SUB_TYPES = ["Sub-Savings", "Vacation Club", "Holiday Club"]


def validate_sub(member, v: dict[str, str]) -> list[str]:
    errors = []
    if v["f_t"] not in SUB_TYPES:
        errors.append("Account Type: Select an account type.")
    if not v["f_n"]:
        errors.append("Nickname: Required.")
    elif not re.fullmatch(r"[A-Za-z0-9 ]{1,20}", v["f_n"]):
        errors.append("Nickname: Letters, digits and spaces only, max 20 characters.")
    funding = next((a for a in member.accounts if a.suffix == v["f_f"]), None)
    if funding is None:
        errors.append("Funding Account: Select a funding account.")
    try:
        amt = Decimal(v["f_d"])
        if amt < Decimal("5.00"):
            errors.append("Initial Deposit: Must be at least $5.00.")
        elif funding is not None and amt > funding.balance:
            errors.append("Initial Deposit: Exceeds available balance in funding account.")
    except InvalidOperation:
        errors.append("Initial Deposit: Enter a dollar amount, e.g. 25.00.")
    if state.conditions.validation and not errors:
        errors.append("Initial Deposit: Exceeds daily transfer limit for this teller.")
    return errors


def create_app() -> FastAPI:
    app = FastAPI(title="MockCore Teller (synthetic)", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def slow(request: Request, call_next):
        if state.conditions.slow_ms and not request.url.path.startswith("/__admin"):
            await asyncio.sleep(state.conditions.slow_ms / 1000)
        return await call_next(request)

    for tenant in TENANTS.values():
        if tenant.prefix:
            app.include_router(make_router(tenant))
    app.include_router(make_router(TENANTS["a"]))

    # ---- test-harness admin endpoints (outside the automation allowlist) ----
    @app.get("/__admin/conditions")
    def get_conditions():
        return state.conditions.model_dump()

    @app.post("/__admin/conditions")
    async def set_conditions(request: Request):
        body = await request.json()
        if not body:
            state.conditions = Conditions()  # clear all toggles
        elif "preset" in body:
            state.conditions = Conditions(**INJECT_PRESETS[body["preset"]])  # exactly one condition
        else:
            state.conditions = state.conditions.model_copy(update=body)
        return state.conditions.model_dump()

    @app.post("/__admin/reset")
    def reset():
        state.reset()
        return JSONResponse({"ok": True})

    @app.get("/__admin/members/{mid}")
    def member_accounts(mid: str):
        m = state.store.members[mid]
        return {"accounts": [{"suffix": a.suffix, "type": a.type, "nickname": a.nickname} for a in m.accounts]}

    return app


app = create_app()

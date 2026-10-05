# Phase 0 — Plan

> Historical: the Phase 0 blueprint, kept for the interview. The built system follows it closely;
> where they differ, the code, [REPORT.md](../REPORT.md) and [DECISIONS.md](../DECISIONS.md) (D16+) are authoritative.

Working design doc. `REPORT.md` is the final, argued version; this file is the
blueprint we build against and may be deleted or folded in at the end.

## 1. Requirements checklist (mapped to the brief)

| # | Brief | Requirement | Where it lands |
|---|---|---|---|
| R1 | 3.1 | Accept goal + target (URL / entry point) | `cua discover --goal --target` |
| R2 | 3.1 | LLM observe → decide → act loop until goal or stop (max steps, timeout, dead-end) | `agent/loop.py`; distinct `StopReason` enum |
| R3 | 3.1 | Real UI interaction; bias to no-clean-DOM | a11y-first observation, frame-piercing, hostile mock app |
| R4 | 3.2 | Typed, serializable artifact = callable capability with a contract | `artifact/schema.py` (Pydantic) → `schemas/capability.schema.json` |
| R5 | 3.2 | Ordered steps; target identification + robustness reasoning | `Step.target.candidates` (priority-ordered, verified unique) |
| R6 | 3.2 | Typed inputs / typed outputs + shape | `inputs[]`, `outputs[]` with type, constraints, sensitivity, normalization |
| R7 | 3.2 | Checkpoint / success condition; versioned; reviewable | per-step `checkpoint`, top-level `success`, semver + status + fingerprint |
| R8 | 3.3 | Replay with no LLM; stable targeting; verify checkpoints; return outputs | `replay/engine.py`; import-ban test |
| R9 | 3.3 | Explicit handling of validation / not-found / permission / dialog / session timeout / slow / failed load | `handlers[]` + detectors run after every step |
| R10 | 3.3 | Result distinguishes business outcome / recoverable / hard failure; debuggable failure | `ReplayResult` discriminated union |
| R11 | 3.4 | Configurable allowlist (domains/routes, action types); agent can't act outside it | `policy.yaml` + single `PolicyGate` |
| R12 | 3.4 | Safe vs risky/irreversible handled conservatively, justified | risk classification + approval via handoff |
| R13 | 3.4 | Never persist secrets / raw PII in artifacts or logs | single `redaction` module + planted-secret tests |
| R14 | 3.5 | Structured log of what + why; richer signal on failure | JSONL per run + screenshot + a11y snapshot (+ restricted trace) |
| R15 | 3.6 | Detect stuck, raise intervention request with context | `handoff/` triggers + persisted `InterventionRequest` |
| R16 | 3.6 | Human takes over *same live session*, hands back, actions recorded, context preserved | control lock + headed browser + CDP endpoint + injected listeners |
| R17 | 3.6 | Know who is in control | `ControlLock` owner + state machine + fencing epoch |
| R18 | 3.7 | Design: surface abstraction (legacy web, desktop) | `Surface` protocol; REPORT section |
| R19 | 3.7 | Design: multi-tenant reuse, drift detection | `app` binding + `tenant_overrides` keyed by step id; drift signals |
| R20 | §5 | Thin-but-real vertical slice of all of the above; cuts documented | `cua demo`; `## Cuts` |
| R21 | §6 | `/README.md` (setup, keys, no-live-services path, demo commands) | Phase 6 |
| R22 | §6 | `/REPORT.md` with the 7 exact headings, ~1–3 pages | Phase 6 |
| R23 | §6 | `/evidence/`: artifact + discovery log + replay log (+ error replay) | `cua demo` populates it |
| R24 | §8 | Stretch: at most one or two | Phase 7 (one) — see note below |
| R25 | §9 | Secrets out of repo; public GitHub repo | `.gitignore`, `.env.example`; you create the remote |

**Stretch-goal accounting.** The prompt pulls two items the brief lists as stretch
into the core: route/value canonicalization (`/member/12345 → /member/:member_id`)
and draft → approved gating. I treat both as *required by* core items rather than
extras — you can't have typed inputs (3.2) without parameterizing observed values,
and approval is how I make irreversible replay conservative (3.4). The single
Phase 7 pick is then the only genuine stretch.

## 2. Architecture

Single process per run, synchronous Playwright, files on disk. No services, no queue.

```
                      ┌──────────────── PolicyGate (policy.yaml) ────────────────┐
                      │ every action, both paths: allow | block | requires_approval │
                      └───────────────────────────────────────────────────────────┘
 goal ─► agent/loop ──► LLMClient (Anthropic | MockLLM)        replay/engine ◄── artifact + params
            │  observe/act                                         │  resolve/act/wait/detect
            ▼                                                      ▼
        ┌──────────────── Surface protocol (no Playwright types leak) ───────────────┐
        │ observe() · act(Action) · count(Locator) · read(Locator) · check(Condition) │
        │ wait_until([Condition]) · snapshot() · current_location()                   │
        └──────────── PlaywrightWebSurface (frames pierced, headed, CDP port) ────────┘
            │                                                      │
     recorder: run → Capability (draft)                 ReplayResult (union) + events
            │                                                      │
            └──────► evidence/ (JSONL, redacted screenshots, a11y snapshots) ◄─────┘
                                  handoff/: ControlLock + InterventionRequest
                                  operator_ui (FastAPI) ⇄ runs/<id>/*.json
```

Key boundaries:

- **Surface seam.** The engine and the artifact speak in `Locator`, `Condition`,
  `Action`, `Observation` — plain Pydantic models. Only `surface/playwright_web.py`
  imports Playwright. A `DesktopSurface` would implement the same protocol over
  UIA/AX (role + name map directly onto `ControlType` + `Name`).
- **Replay never sees the LLM.** `cua.replay` may not import `cua.agent` or
  `anthropic`; enforced by an AST test and a subprocess `sys.modules` test.
- **Ordering of locator candidates is engine logic, not surface logic.** The engine
  asks the surface "how many matches for this locator?" and picks the first unique
  one — so drift detection is identical across surfaces.
- **Control lock is checked inside the surface wrapper** (`GuardedSurface.act`), so
  no code path can act without holding it.
- **Intervention state is files with one writer per file**: the runner writes
  `request.json`, the operator writes `decision.json`. No locking or DB needed, and
  the operator UI can be a separate process.

## 3. Draft artifact schema (Pydantic v2)

```python
SchemaVersion = Literal["1.0"]
class Sensitivity(StrEnum):  PUBLIC; INTERNAL; PII; SECRET
class RiskClass(StrEnum):    SAFE; REVERSIBLE; IRREVERSIBLE
class Status(StrEnum):       DRAFT; APPROVED; DEPRECATED

# ---- contract: inputs / outputs / outcomes ---------------------------------
class Constraints(BaseModel):
    pattern: str | None = None; min_length: int | None = None; max_length: int | None = None
    minimum: Decimal | None = None; maximum: Decimal | None = None; enum: list[str] | None = None

class InputParam(BaseModel):
    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    type: Literal["string", "integer", "decimal", "date", "enum"]
    description: str
    required: bool = True
    constraints: Constraints | None = None
    sensitivity: Sensitivity
    secret_ref: str | None = None        # SECRET inputs: a credential *name*, never a value
    example: str | None = None           # synthetic; never the discovery value for PII

class Extraction(BaseModel):
    step_id: str                         # step whose post-state holds the value
    target: Target
    attribute: Literal["text", "value"] = "text"

class OutputField(BaseModel):
    name: str; type: Literal["string", "decimal", "integer", "date", "boolean"]
    description: str; sensitivity: Sensitivity
    source: Extraction
    normalize: Literal["none", "trim", "currency_to_decimal", "mdy_to_iso_date"] = "trim"
    required: bool = True

class OutcomeSpec(BaseModel):            # business outcomes are part of the signature,
    name: str                            # like checked exceptions: "member_not_found"
    description: str
    detail_fields: list[str] = []        # e.g. ["field_errors"]

# ---- targeting ---------------------------------------------------------------
class RoleName(BaseModel):  strategy: Literal["role_name"];  role: str; name: str   # name may hold {param}
class Label(BaseModel):     strategy: Literal["label"];      text: str
class NearLabel(BaseModel): strategy: Literal["near_label"]; label_text: str; control_role: str
class TextAnchor(BaseModel):strategy: Literal["text"];       text: str; role: str | None = None
class TableCell(BaseModel): strategy: Literal["table_cell"]; column_header: str
                            row_key_column: str; row_key_value: str                # "{member_id}"
class Css(BaseModel):       strategy: Literal["css"];        selector: str
class XPath(BaseModel):     strategy: Literal["xpath"];      expr: str
Locator = Annotated[RoleName | Label | NearLabel | TextAnchor | TableCell | Css | XPath,
                    Field(discriminator="strategy")]

class FrameRef(BaseModel):  name: str | None = None; url_pattern: str | None = None
class Target(BaseModel):
    description: str                     # "Search button" — for humans and logs
    frame_path: list[FrameRef] = []      # [] = top document
    candidates: list[Locator]            # priority order; each verified unique at record time

# ---- conditions (checkpoints, preconditions, detectors share one language) ---
class RouteMatches(BaseModel):   kind: Literal["route_matches"];   pattern: str     # "/member/:member_id"
class ElementVisible(BaseModel): kind: Literal["element_visible"]; target: Target
class TextVisible(BaseModel):    kind: Literal["text_visible"];    text: str; frame_path: list[FrameRef] = []
class FieldValue(BaseModel):     kind: Literal["field_value"];     target: Target; equals: str
class HttpStatus(BaseModel):     kind: Literal["http_status"];     min: int; max: int
Condition = Annotated[..., Field(discriminator="kind")]

# ---- steps ---------------------------------------------------------------------
class RetryPolicy(BaseModel): max_attempts: int = 2; backoff_ms: list[int] = [500, 2000]
class Step(BaseModel):
    id: str                              # stable slug; overrides + logs key on this, not index
    description: str
    action: Literal["navigate", "click", "type", "select", "wait_for", "extract"]
    target: Target | None = None
    value: str | None = None             # template: "{member_id}", "/member/:member_id"
    secret_ref: str | None = None        # typing a credential by reference
    risk: RiskClass
    precondition: list[Condition] = []
    checkpoint: list[Condition]          # all must hold after the step
    timeout_ms: int = 10_000
    retry: RetryPolicy | None = None     # validator: forbidden when risk == IRREVERSIBLE
    performed_by: Literal["agent", "human"] = "agent"

# ---- exception handlers ----------------------------------------------------------
class ReturnOutcome(BaseModel): type: Literal["return_outcome"]; outcome: str
                                details: list[OutputField] = []       # e.g. per-field error text
class Dismiss(BaseModel):       type: Literal["dismiss"]; target: Target; max_times: int = 3
class Retry(BaseModel):         type: Literal["retry"]; policy: RetryPolicy
class Reauthenticate(BaseModel):type: Literal["reauthenticate"]; max_times: int = 1
class Escalate(BaseModel):      type: Literal["escalate"]; reason: str
class Fail(BaseModel):          type: Literal["fail"]; error_code: str
class Handler(BaseModel):
    id: str; description: str
    detect: list[Condition]              # all must hold
    category: Literal["business_outcome", "recoverable", "hard_failure"]
    applies_to: list[str] | Literal["*"] = "*"    # step ids
    response: ReturnOutcome | Dismiss | Retry | Reauthenticate | Escalate | Fail
    # validator: business_outcome ⇔ return_outcome; recoverable ⇔ dismiss|retry|reauthenticate;
    #            hard_failure ⇔ fail|escalate

# ---- binding, session, idempotency, provenance ------------------------------------
class TenantOverride(BaseModel):
    base_url: str | None = None; route_prefix: str | None = None
    text_map: dict[str, str] = {}                     # "Member ID" → "Member #"
    step_overrides: dict[str, list[Locator]] = {}     # step_id → candidates tried first
    extra_handlers: list[Handler] = []
class AppBinding(BaseModel):
    vendor_product: str; app_version_range: str       # ">=4.2,<5"
    surface: Literal["web", "legacy_web", "desktop"]
    entry_point: str                                  # relative to tenant base_url
    tenant_overrides: dict[str, TenantOverride] = {}
class SessionSpec(BaseModel):
    login_route: str; login_steps: list[Step]; logged_in: list[Condition]
    credential_refs: list[str]                        # names only
class Idempotency(BaseModel):
    key_inputs: list[str]                             # e.g. ["member_id", "nickname"]
    probe_steps: list[Step]                           # read-only steps
    exists: list[Condition]                           # "row with Nickname == {nickname} visible"
class Provenance(BaseModel):
    discovery_run_id: str; model: str; recorded_at: datetime; recorder_version: str
    goal_redacted: str; redactions_applied: list[str]
    human_steps: list[str] = []                       # step ids performed by an operator
    reviewed_by: str | None = None; approved_at: datetime | None = None
    parent_version: str | None = None

class Capability(BaseModel):
    schema_version: SchemaVersion
    capability_id: str                                # "mockbank.member.read_savings_balance"
    version: str                                      # semver: MAJOR = contract change
    status: Status
    title: str; description: str
    app: AppBinding
    inputs: list[InputParam]; outputs: list[OutputField]; outcomes: list[OutcomeSpec]
    session: SessionSpec | None
    steps: list[Step]; handlers: list[Handler]
    success: list[Condition]
    idempotency: Idempotency | None = None            # required if any step is IRREVERSIBLE
    provenance: Provenance
    fingerprint: str                                  # sha256 of canonical steps+handlers+contract
    # validators: every {param} referenced is declared; step ids unique; outcome names in
    # handlers ⊆ outcomes; extraction step_ids exist; IRREVERSIBLE ⇒ idempotency present.
```

**Why this shape.** Contract first (inputs/outputs/*outcomes*) so a calling agent can
treat it as a typed function including its "checked exceptions". Steps keyed by
stable ids so tenant overrides and drift reports survive reordering. One `Condition`
language for checkpoints, preconditions and error detectors keeps the replay engine
small. Locators are semantic first (role/name, label) because those map onto any
accessibility API, including desktop.

**Where handlers come from.** Discovery sees the happy path, so it can't learn the
error states. Error signatures are vendor-product knowledge ("MockCore shows
`No member found` in the results frame"). They live in an app profile
(`apps/mockcore.yaml`) authored once per vendor product. The recorder copies the
relevant ones into the artifact, so the artifact is self-contained and versioned.
This is also the multi-tenant lever: one profile serves every tenant on that
product.

**Parameterization.** On `finish`, the LLM proposes inputs and outputs (name, type,
sensitivity, the value it observed). The recorder only *accepts* a proposed input
if its observed value literally appears in a typed value, a URL, or a locator. It
then substitutes `{name}` deterministically. The LLM suggests; code verifies.

**Versioning.** `schema_version` is the file format. `version` is the capability's
semver. MAJOR changes the input/output/outcome contract, MINOR changes steps,
locators or handlers compatibly, PATCH is metadata only. Approved artifacts are
immutable, and edits write a new file at `artifacts/<capability_id>/<version>.json`.

## 4. Replay result contract + error taxonomy

```python
class LocatorAttempt(BaseModel): strategy: str; match_count: int | None; error: str | None
class ReplayEvent(BaseModel):    t_ms: int; step_id: str | None; kind: str; detail: dict
class _Base(BaseModel):
    run_id: str; capability_id: str; version: str; duration_ms: int
    events: list[ReplayEvent]; drift: list[DriftSignal]; evidence_dir: str
class Success(_Base):         status: Literal["success"];          outputs: dict[str, Any]
                              already_existed: bool = False        # idempotency probe hit
class BusinessOutcome(_Base): status: Literal["business_outcome"]; outcome: str
                              step_id: str; details: dict[str, Any]
class NeedsHuman(_Base):      status: Literal["needs_human"];      intervention_id: str
                              reason: str; step_id: str | None
class Failure(_Base):         status: Literal["failure"];          error_code: ErrorCode
                              step_index: int | None; step_id: str | None
                              expected: str; observed: str
                              locator_attempts: list[LocatorAttempt]; evidence: EvidencePaths
ReplayResult = Annotated[Success | BusinessOutcome | NeedsHuman | Failure,
                         Field(discriminator="status")]

ErrorCode = Literal["invalid_input", "policy_blocked", "not_approved", "target_not_found",
                    "target_ambiguous", "checkpoint_failed", "timeout", "app_error",
                    "unrecognized_state", "reauth_failed", "idempotency_ambiguous",
                    "aborted_by_operator", "app_version_mismatch"]
```

| Runtime condition (injectable) | Detected by | Category | Response | Caller sees |
|---|---|---|---|---|
| Member not found | `text_visible "No member found"` after search | business outcome | return | `BusinessOutcome(member_not_found)` |
| Validation error on form | error cell next to field | business outcome | return + extract field errors | `BusinessOutcome(validation_error, {field_errors})` |
| Permission denied page | heading `Access Denied` | business outcome | return | `BusinessOutcome(permission_denied)` |
| Maintenance interstitial | dialog text + `OK` button | recoverable | dismiss (≤3), continue | `Success` (event logged) |
| Session expiry → login | route `/login` mid-flow | recoverable | re-auth from credential ref (≤1), restart flow | `Success` (event logged) or `Failure(reauth_failed)` |
| Slow page | checkpoint not yet true | — | explicit wait up to `timeout_ms` | `Success`, or `Failure(timeout)` |
| Intermittent 500 | `http_status 500–599` / error page | recoverable | retry with backoff (≤2) on safe steps only | `Success` or `Failure(app_error)` |
| Unknown modal (extra, for the stuck demo) | nothing matches; checkpoint fails | — | escalate if an operator is attached, else fail | `NeedsHuman` / `Failure(unrecognized_state)` |
| Bad param (`member_id=abc`) | input validation before UI | — | — | `Failure(invalid_input)` |

**Order after each step:** wait until *any* of {checkpoint, applicable detectors}
becomes true (bounded by `timeout_ms`). Detectors are evaluated first. Then handle
the result, or verify the checkpoint. Waits poll conditions; there are no fixed
sleeps.

**Idempotency (write flow).** The submit step is `IRREVERSIBLE`, so it is never
auto-retried. Before it runs, and again after any ambiguous outcome (500, timeout,
or session loss after the click), the engine runs `idempotency.probe_steps`, which
check the member's sub-account list for `nickname == {nickname}`. If the account
exists, the result is `Success(already_existed=True)`. If the outcome was ambiguous
and the account doesn't exist, the result is `NeedsHuman`, never a blind
re-submit.

## 5. Handoff state machine

```
                 escalate(reason)                take_control
   ┌─────────┐ ───────────────► ┌───────────────────┐ ───────────► ┌──────────────────┐
   │ RUNNING │                  │ PAUSED_FOR_HUMAN  │              │ HUMAN_IN_CONTROL │
   │owner=auto│ ◄──┐            │ owner=none        │              │ owner=human      │
   └────┬────┘    │            └──┬──────────┬─────┘              └───┬──────────┬───┘
        │ done    │ verified       │ approve   │ abort                   │ resume   │ abort
        ▼         │                ▼           ▼                         ▼          ▼
   COMPLETED  ┌───┴──────┐ ◄───────┘       ABORTED ◄─────────────────────┼──── ABORTED
              │ RESUMING │ ◄─────────────────────────────────────────────┘
              │owner=none│ ── mismatch, no later checkpoint matches ──► PAUSED_FOR_HUMAN (≤1) / FAILED
              └──────────┘
```

- Every transition is an explicit method with an actor. Illegal transitions raise
  `IllegalTransition`, which tests cover.
- The lock carries a monotonically increasing **epoch** (fencing token). Automation
  captures the epoch when it acquires the lock, and `GuardedSurface.act` rejects any
  action whose epoch is stale. This prevents a slow automation thread from acting
  after a human took over.
- **Triggers:**
  - discovery: `request_human`, no progress (same observation hash 3×), step budget
    at 80%, policy `requires_approval`
  - replay: an `escalate` handler, unrecognized state, `requires_approval`, an
    ambiguous idempotency check
- **Same session.** Chromium runs headed with a CDP endpoint. The human uses that
  very window. The `cua demo` scripted operator connects to the *same* browser over
  CDP from a separate process, which proves it is not a fresh session.
- **Action capture.** An init script installed on the context, so it runs in every
  frame, listens for click/input/change events in the capture phase. It reports
  role, name and label (values redacted) through an exposed binding. The runner
  polls `decision.json` with `page.wait_for_timeout`, which keeps Playwright's event
  loop pumping so the binding callbacks fire during the pause. The trace keeps
  running across the handoff.
- **Resume.** Re-observe, then check the last verified checkpoint. If a *later*
  step's checkpoint holds instead (the human did steps), skip ahead and log it. If
  the app is on the login page, re-authenticate. If nothing matches, re-escalate
  once, then fail with `unrecognized_state`. In discovery, the LLM simply
  re-observes, and human actions become `performed_by: human` steps flagged for
  review.

## 6. Policy sketch

```yaml
allowed_origins: ["http://localhost:8000"]
allowed_routes: ["/login", "/app/**", "/member/**", "/subaccount/**"]   # /__admin/** not allowed
allowed_actions: [navigate, click, type, select, wait_for, extract, request_human, finish]
irreversible:                         # classification is policy's job, never the LLM's
  - {action: click, route: "/subaccount/review", name_pattern: "(?i)^submit"}
  - {action: click, name_pattern: "(?i)^(submit|confirm|post|delete|transfer)\\b"}
discovery:  {on_irreversible: requires_approval, on_block: stop}
replay:     {irreversible_requires: [artifact_approved, allow_irreversible_flag],
             allow_reauth: true}
capabilities:
  mockbank.subaccount.open: {max_runs_per_hour: 20}
redaction:  {pii_fields: [ssn, dob, full_name, phone, email, address], mask_member_id: last2}
```

An artifact cannot downgrade risk. At replay, the gate re-classifies every step,
and the stricter of the artifact and the policy wins.

## 7. Risks I'm tracking

1. **Second CDP client into a Playwright-launched Chromium**
   (`--remote-debugging-port`). I expect this to work. Fallback: launch Chromium
   myself and have both automation and operator `connect_over_cdp`.
2. **`claude-opus-5-5` rejects forced `tool_choice`.** Use `auto` with
   `disable_parallel_tool_use`, `strict: true` tools and a prompt instruction. A
   text-only reply gets one nudge and counts toward no-progress.
3. **Screenshot masking inside iframes.** Playwright `mask` with frame-scoped
   locators. Fallback: inject a blur style on tagged elements per frame.
4. **Playwright traces can't be redacted.** They hold DOM snapshots with PII. Traces
   go to `runs/<id>/restricted/` (gitignored, documented as restricted evidence),
   not to `evidence/`.
5. **Python 3.14 wheel availability.** Pin the project to 3.12.

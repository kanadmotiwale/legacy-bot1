# Design report

Built against a synthetic, deliberately hostile legacy bank app: a frameset with a nested iframe, table
layouts, generated class names, unlabeled inputs, and two tenants. `cua demo` runs 19 scenarios end to
end; the results are in [evidence/SUMMARY.md](evidence/SUMMARY.md) and the decision log in
[DECISIONS.md](DECISIONS.md).

## Architecture

```
goal ─► agent/loop ─► LLMClient (Anthropic | MockLLM)        artifact + params ─► replay/engine (no LLM)
            └────── every action ─► PolicyGate: allow | block | requires_approval ◄─────┘
       Surface protocol: observe · act · match(locator) · check(condition) · read · screenshot
            └─────────── PlaywrightWebSurface (frames pierced, CDP endpoint) ──────────┘
  recorder: run → Capability    handoff: ControlLock + InterventionRequest ⇄ operator UI
```

- **One process per run, synchronous Playwright, files on disk.** Nothing inside a run needs
  concurrency, so to scale you add workers rather than infrastructure.
- **The seam.** The engine, recorder and artifact speak only in `Locator`, `Condition`, `Observation` and
  `ActionRequest`. Only the surface and its in-page `dom.js` touch the browser. The engine decides the
  order in which locators are tried and the surface only counts matches, so fallback and drift detection
  are surface-independent.
- **Accessibility-first perception.** A per-frame walker emits each element's role, name, label, adjacent
  label cell and table column/row. The model sees that plus a screenshot, but acts by element ref, not
  coordinates: a click at (412, 380) records *where*, and replay needs *what*. Role, name, label and grid
  position are also what UIA and AX expose on desktop.
- **Vendor app profiles** hold product knowledge: sign-on, error signatures, the idempotency probe. They
  are authored once per product and copied into each artifact. Discovery only sees the happy path, so it
  can't learn error states, and one profile serves every tenant on that product.

## Artifact schema

A callable contract, not a transcript. See the
[read-balance artifact](evidence/artifacts/mockcore.member.read_savings_balance-1.0.0.json) and the
[JSON Schema](schemas/capability.schema.json).

- **Contract.** Typed `inputs` with constraints and a `sensitivity`; secrets are refused as inputs
  because they are credential references. Typed, normalized `outputs`. Declared business `outcomes`
  such as `member_not_found`, which work like checked exceptions.
- **Steps** have a stable `id` (overrides and drift reports key on it, never on position), a `target`
  (a frame path plus priority-ordered candidates), a `value` template, a `risk` class, a `precondition`
  and a post-step `checkpoint`.
- **One condition language** (`route_matches`, `text_visible`, `element_visible`, `field_value`,
  `http_status`) serves checkpoints, preconditions, the success condition and error detectors.
- **Handlers** map a detector to a category and a response. Validation forbids nonsense, such as
  "retry" for a business outcome.
- **Binding and lifecycle.** Vendor product, version range with a version probe, surface,
  `tenant_overrides`, authored `session` steps, a mandatory `idempotency` probe if any step is
  irreversible, and `provenance` with no transcript. A `fingerprint` plus semver (MAJOR = contract
  change, MINOR = behaviour change). `draft` → `approved`; approved versions are immutable.
- **Parameterization: the LLM suggests, code verifies.** A proposed input is accepted only if its value
  literally appears in the recorded flow, then substituted deterministically (`10042` → `{member_id}`,
  `/member/10042` → `/member/:member_id`). A leak check refuses to save any known-sensitive value.

## Determinism & error handling

- **No LLM on the replay path.** A test walks the import closure of `cua.replay` and fails if it reaches
  `anthropic` or `cua.agent`.
- **Locators.** At record time every candidate is verified to be unique *and* to hit the element the agent
  chose. The order is role + name, label, adjacent label cell (legacy forms rarely associate labels),
  text, table structure, CSS. Two exceptions: a control repeated per row ("View") is anchored by its row
  first, because role + name is only unique by accident; and data cells are never anchored by their own
  text. At replay the first unique candidate wins. A lower-priority match is accepted only after an
  800 ms grace period, so a loading page can't push us onto CSS, and it is logged as a **drift signal**.
- **Waits.** After each action the engine waits, with a deadline, for a detector *or* the checkpoint,
  detectors first. Detectors also run before each step. A checkpoint that was already true before a
  click only counts after a navigation. The only timed delay is retry backoff.

| Condition | Category | Response | Caller gets |
|---|---|---|---|
| No member / restricted member | business | return | `BusinessOutcome(member_not_found \| permission_denied)` |
| App rejects form values | business | return field details | `BusinessOutcome(validation_error, {field_errors})` |
| Maintenance interstitial | recoverable | dismiss (≤3) | `Success`, event logged |
| Session expiry | recoverable | re-auth from credential ref (≤1), restart | `Success` or `Failure(reauth_failed)` |
| HTTP 5xx | recoverable | backoff, reload frame (≤2) | `Success` or `Failure(app_error)` |
| Bad parameter | none | rejected before any UI | `Failure(invalid_input)` |
| Unknown state | none | escalate | `NeedsHuman(intervention_id)` |

- **Four result cases:** `Success`, `BusinessOutcome`, `NeedsHuman`, `Failure`. A `Failure` reports the
  step, the expected checkpoint, what was observed (every frame's URL and status), locator attempts and
  masked evidence. "The artifact doesn't fit this UI" is a `Failure` (it needs re-recording);
  "the UI is in an unknown state" is `NeedsHuman` (an operator can resolve it).
- **Write-flow idempotency.** Submit is irreversible by policy and can't carry a retry policy. Before
  starting, and after any ambiguous outcome, the engine probes the member's account list for the
  caller's nickname. If found, the result is `Success(already_existed=true)`; if it's ambiguous and not
  found, a human decides. Nothing is ever blindly re-submitted.

## Heterogeneity & multi-tenant

- **Surfaces.** Only the surface, `dom.js`, the `css` locator and URL routes are web-specific. A
  **`DesktopSurface`** builds the same element model from UIA or AX (ControlType → role, LabeledBy →
  label, Grid pattern → `table_cell`, window chain → `frame_path`), acting via Invoke/Value patterns with
  coordinates as a fallback. A **`LegacyWebSurface`** with no tree at all (Citrix, canvas) observes by
  screenshot + OCR and adds `{ocr_text, near_text, offset}` locators, still verified unique and ordered
  last. The engine and schema don't change beyond the `surface` field and new locator types.
- **Tenants.** Artifacts are recorded per vendor product + version range and are tenant-neutral. A
  tenant is a small `TenantOverride`: base URL, route prefix, an exact-match `text_map`, per-step
  candidate overrides and extra handlers, applied at run time. The demo replays the *same* artifact on
  tenant "lakeshore" via a 5-line override, despite a `/tb` prefix, relabels, an extra column and a newer
  version. Table anchors use header names, so the extra column costs nothing.
- **Drift.** A version probe refuses unsupported versions. Every run records which locator matched, so a
  lower-priority match flags the step, tenant and version *before* anything breaks, and a broken
  checkpoint names exactly where things diverged. The demo's unmapped-tenant run shows both. Designed but
  not built: per-tenant canary replays and re-discovery of only the degraded step.

## Escalation & handoff

- **Stuck detection.** In discovery: `request_human`, an unchanged screen 3 times running, 80% of the step
  budget used, or an approval required. In replay: a deadline with no detector, a dismiss loop, a
  required approval, or an ambiguous irreversible outcome.
- **Routing.** A persisted `InterventionRequest` carries the goal or capability, step, reason, URLs, a
  masked screenshot, recent events, the proposed action and the CDP endpoint. The runner writes the
  request and operators append decisions; one writer per file, so no locking.
- **Control.** A `ControlLock` state machine (RUNNING → PAUSED_FOR_HUMAN → HUMAN_IN_CONTROL → RESUMING →
  RUNNING) gives each state an owner. Every automation action checks the state *and* a **fencing epoch**
  that increments when automation regains control, so an action decided before a handoff can't land
  after it. Illegal operator decisions are rejected.
- **Same live session.** The run's Chromium exposes a CDP endpoint. In the demo a *separate process*
  attaches and clicks, proving the session isn't fresh. Injected listeners record the human's clicks and
  edits (redacted), and only while the human holds the lock.
- **Handing back.** Resume re-observes and scans back from the last step for a checkpoint that already
  holds; if one does, it skips ahead. Otherwise it re-runs the current step if its precondition holds,
  else fails. It never assumes where the human left the app.

## Safety

- **Default-deny allowlist** of origins, routes and actions, checked against both the destination and
  the document being acted on. The harness's admin routes are unreachable.
- **Risk is classified by policy, never the model**; an artifact can raise a step's risk but not lower
  it.
- **Irreversible actions** need operator approval in discovery. In replay they need an approved
  artifact, an explicit `--allow-irreversible` and an allowlisted capability, and are never
  auto-retried. Blocking outright would make the flow unrecordable; a flag alone is too weak for money
  movement.
- **Credentials** are references resolved at use, and the model never signs on, so they never enter a
  prompt.
- **Redaction** is one module covering every log, request, snapshot and result: known values (secrets,
  inputs, values learned from sensitive screen fields), field-rule masks on observations and screenshots,
  and regexes as a backstop. The model is also kept from fields no task needs (SSN, DOB, phone, address).
  Tests grep all output for planted secrets.
- **Limits.** The model still sees names and balances, which go to the provider. Free-text PII can slip
  through. Traces hold unredacted DOM (kept in a gitignored folder). The lock is cooperative, the console
  is unauthenticated, and prompt injection is mitigated, not solved.

## Cuts

- **No live LLM run.** No API key was available. The Anthropic client is tested against a fake SDK
  client, and every recorded run uses MockLLM behind the same interface, as the brief permits ("mock the
  boundary cleanly").
- **One surface.** Desktop and OCR surfaces are designed only.
- **Minimal console:** polling, no authentication, no queue or streamed co-browsing.
- **No assisted single-step LLM recovery on replay failure.**
- **Simplifications:** human steps recorded in discovery are flagged rather than verified; the flow
  restarts from step 0 after re-auth; file storage; naive rate limits.
- **Next:** live-model evaluation, assisted step recovery, canary replays with a drift dashboard,
  isolated remote browsers, and a UIA desktop prototype.

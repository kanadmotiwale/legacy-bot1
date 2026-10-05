# Design report

I built this against a synthetic, deliberately hostile legacy bank app: a frameset with a nested iframe,
table layouts, generated class names, unlabeled inputs, and two tenants. `cua demo` runs 19 scenarios
from goal, through LLM discovery, artifact and deterministic replay, to typed results and same-session
human handoff. The results are in [evidence/SUMMARY.md](evidence/SUMMARY.md). The decision log is in
[DECISIONS.md](DECISIONS.md).

## Architecture

```
goal ─► agent/loop ─► LLMClient (Anthropic | MockLLM)          artifact + params ─► replay/engine (no LLM)
            └────── every action ─► PolicyGate: allow | block | requires_approval ◄──────┘
       Surface protocol: observe · act · match(locator) · check(condition) · read · screenshot
            └──────────── PlaywrightWebSurface (frames pierced, CDP endpoint) ───────────┘
  recorder: run → Capability    handoff: ControlLock + InterventionRequest ⇄ operator UI
  evidence: redacted JSONL, masked screenshots, a11y snapshots (Playwright traces kept restricted)
```

- **One process per run, synchronous Playwright, files on disk.** Nothing inside a run needs concurrency,
  and scaling means more independent workers. The brief rewards a correct core over infrastructure.
- **The seam.** The engine, recorder and artifact speak only in `Locator`, `Condition`, `Observation`
  and `ActionRequest`. Only `playwright_web.py` and the in-page `dom.js` touch the browser. The engine
  owns the order in which locators are tried; the surface only counts matches. Fallback order and drift
  detection are therefore identical on any surface.
- **Accessibility-first perception, not pixels.** A per-frame walker emits each element's role,
  accessible name, label, *adjacent label cell* and *table column/row context*. The model also gets a
  screenshot but acts by element ref. I rejected coordinate-based computer use: a click at (412, 380)
  records where, not what, and replay needs semantic, reviewable targets. Role, name, label and grid
  position are also what UIA and AX expose on desktop.
- **Vendor app profiles** hold product-level knowledge: sign-on, error signatures, the idempotency probe.
  They are authored once per product and copied into each artifact at record time. Discovery only sees
  the happy path, so it can't learn error states, and one profile serves every tenant on that product.

## Artifact schema

The artifact is a callable contract, not a transcript. See the
[read-balance artifact](evidence/artifacts/mockcore.member.read_savings_balance-1.0.0.json), the
[write-flow artifact](evidence/artifacts/mockcore.subaccount.open-1.0.0.json) and the
[JSON Schema](schemas/capability.schema.json).

- **Contract.** It has:
  - typed `inputs` with constraints and a `sensitivity`. Secrets are rejected as inputs; they are
    credential references resolved at use.
  - typed, normalized `outputs`: `$12,345.67` becomes `12345.67`.
  - declared business `outcomes` such as `member_not_found`. These work like checked exceptions, so the
    caller knows every answer it can get.
- **Steps** carry a stable `id`, a `target` (a `frame_path` plus priority-ordered `candidates`), a `value`
  template, a `risk` class, a `precondition` and a post-step `checkpoint`. Overrides, drift reports and
  logs key on the id, never on the step's index.
- **One condition language.** `route_matches`, `text_visible`, `element_visible`, `field_value` and
  `http_status` serve checkpoints, preconditions, the final success condition and error detectors alike.
- **Handlers** map a detector to a category and a response. Validation forbids nonsense pairings:
  business outcomes must return an outcome, recoverable conditions must dismiss, retry or re-authenticate,
  and hard failures must fail or escalate.
- **The rest:**
  - a binding: vendor product, version range with a version probe, surface, entry point, and
    `tenant_overrides`
  - `session`: authored sign-on steps using credential refs
  - `idempotency`: a read-only probe, mandatory if any step is irreversible
  - `provenance`: run id, model, redactions, human-performed steps and approval; never a transcript
  - a `fingerprint`, and semver (MAJOR = contract change, MINOR = behaviour change)
  - `draft` → `approved`, with approved versions immutable
- **Parameterization: the LLM suggests, code verifies.** At `finish` the model proposes inputs with the
  values it used. An input is accepted only if that literal appears in the recorded flow, then
  substituted deterministically: `10042` becomes `{member_id}`, and `/member/10042` becomes
  `/member/:member_id`. A leak check refuses to save an artifact containing any value known to be
  sensitive.

## Determinism & error handling

- **No LLM on the replay path, enforced structurally.** A test walks the import closure of `cua.replay`
  and fails on `anthropic` or `cua.agent`. A subprocess test confirms neither is ever loaded.
- **Choosing locators at record time.** Every candidate is proposed and verified against the live page
  to be unique *and* to resolve to the element the agent chose. The priority order is: role + name,
  label, adjacent label cell (legacy forms rarely associate labels), text, table structure, CSS.
  There are two deliberate exceptions:
  - A control repeated per row, such as a "View" link, is anchored by its row first ("link in the row
    whose *Member ID* is `{member_id}`"), because role + name is unique only by accident.
  - Data cells are never anchored by their own text, because that text is the data.
- **Resolving locators at replay time.** Replay takes the first candidate with a unique match. A
  lower-priority match is accepted only after an 800 ms grace period, so a loading page can't push us
  onto CSS. It is logged as a **drift signal**. If nothing matches uniquely, the result is a `Failure`
  listing every attempt and its match count.
- **Waits are explicit.** After each action the engine waits, with a deadline, for *either* a detector
  or the checkpoint, and detectors are always evaluated first. They also run before each step and while
  waiting for a target. A checkpoint that was already true before a click only counts after a
  navigation. The only timed delay is retry backoff.

| Condition | Detected by | Category | Response | Caller gets |
|---|---|---|---|---|
| No member / restricted member | page text | business | return | `BusinessOutcome(member_not_found \| permission_denied)` |
| App rejects form values | error banner + field lines | business | return with details | `BusinessOutcome(validation_error, {field_errors})` |
| Maintenance interstitial | "System Notice" | recoverable | dismiss (≤3) | `Success`, event logged |
| Session expiry | "session has expired" | recoverable | re-auth from credential ref (≤1), restart | `Success`, or `Failure(reauth_failed)` |
| HTTP 5xx | per-frame document status | recoverable | backoff, reload frame (≤2) | `Success`, or `Failure(app_error)` |
| Slow page | checkpoint pending | none | wait to deadline | `Success` |
| Bad parameter | typed validation, before any UI | none | none | `Failure(invalid_input)` |
| Unknown state | deadline, no detector | none | escalate | `NeedsHuman(intervention_id)` |

- **The result is a four-case union:** `Success`, `BusinessOutcome`, `NeedsHuman` and `Failure`. A
  `Failure` gives the step id and index, the *expected* checkpoint in readable form, what was *observed*
  (every frame's URL and status), locator attempts, and a masked screenshot plus accessibility snapshot.
- **Two failure kinds stay separate.** "The artifact doesn't fit this UI" is a `Failure`: it needs
  re-recording or an override. "The UI is in a state the artifact doesn't know" is `NeedsHuman`: an
  operator can resolve it.
- **Idempotency for the write flow.**
  - The submit step is irreversible by policy, and the schema forbids it a retry policy.
  - Before starting, and after any ambiguous outcome (a 5xx, timeout or lost session after the click),
    the engine probes the member's account list for the caller's nickname. If it's found, the result is
    `Success(already_existed=true)` and nothing is re-submitted.
  - If the outcome is ambiguous and the account isn't found, a human decides. The engine never blindly
    re-submits.
  - Legacy apps have no idempotency keys, so a caller-supplied business key is the honest substitute.

## Heterogeneity & multi-tenant

- **Surface abstraction.** Only `PlaywrightWebSurface`, `dom.js`, the `css` locator and URL-based routes
  are web-specific. Steps, handlers, conditions, results and the engine are surface-neutral.
  - **`DesktopSurface`** builds the same `UIElement` from UIA or AX. ControlType becomes role, Name
    becomes name, LabeledBy becomes label, Grid/Table patterns become `table_cell`, and the window/pane
    chain becomes `frame_path`. It acts via the Invoke/Value patterns, with coordinates as a fallback.
  - **`LegacyWebSurface`** (Citrix, canvas, applets: no tree at all) observes by screenshot plus OCR and
    adds locator variants like `{ocr_text, near_text, offset}`. These are still verified unique and
    ordered last.
  - Neither needs changes to the engine or the schema beyond the `surface` field and new locator types.
- **Multi-tenant reuse.**
  - Artifacts are recorded per *vendor product + version range* and are tenant-neutral.
  - A tenant is a small `TenantOverride` delta: base URL, route prefix, an exact-match `text_map` for
    relabels, `step_overrides` keyed by step id (extra candidates tried first), and `extra_handlers`.
    `bind()` applies it at run time.
  - The demo replays the *same* artifact on tenant "lakeshore" via a 5-line override. Lakeshore has a
    `/tb` prefix, relabeled fields and buttons, a renamed balance column, an extra column and version
    4.4. Table anchors use header names, not column indexes, so the extra column costs nothing.
  - Layering goes product, then version (a MINOR bump), then tenant, each reviewable as a diff.
- **Detecting drift.**
  - A version probe refuses to replay outside the artifact's range.
  - Every run records which candidate matched, so a lower-priority match flags a degraded step, tenant
    and version *before* anything breaks.
  - A broken checkpoint names exactly where things diverged.
  - The demo's unmapped-tenant run shows all three: the CSS fallback is flagged on two steps, then the
    run fails at the precise checkpoint the missing relabel broke.
  - Next, designed but not built: per-tenant canary replays feeding a drift dashboard, and re-discovery
    of *only the degraded step* to propose a reviewed `step_override`.

## Escalation & handoff

- **Detecting "stuck".**
  - In discovery: `request_human`, an unchanged observation 3 times in a row, 80% of the step budget
    used, no uniquely targetable element, or `requires_approval`.
  - In replay: a checkpoint or precondition deadline with no detector, a dismiss loop, a required
    approval, an ambiguous irreversible outcome, or re-authentication forbidden.
- **Routing.** A persisted `InterventionRequest` carries the capability or goal, the step, the reason,
  the URL, a masked screenshot plus accessibility snapshot, recent events, the proposed action, what
  automation expects after resume, the CDP endpoint and the allowed decisions. The runner writes
  `request.json` and operators append to `decisions.jsonl`. With one writer per file there is no locking,
  and the console is a separate process.
- **Who is in control.** A `ControlLock` state machine (RUNNING → PAUSED_FOR_HUMAN → HUMAN_IN_CONTROL →
  RESUMING → RUNNING, or ABORTED / FAILED) gives each state an explicit owner.
  - Every automation action passes `GuardedSurface.act`, which checks the state *and* a **fencing epoch**
    that increments whenever automation reacquires control. An action decided before a handoff can never
    land after it.
  - Operator decisions are validated against the lock. Illegal transitions are rejected, logged and
    tested.
- **Same live session.** The run's Chromium is headed on request and always exposes a CDP endpoint. A
  human uses that window or attaches to the endpoint. The demo operator is a *separate process* that
  attaches over CDP and clicks, which proves this is not a fresh session. An init script in every frame
  reports semantic click and change events (redacted) through a binding. They are recorded only while a
  human holds the lock, and the trace runs across the handoff.
- **Handing back.** Resume never assumes the human left things where we expect.
  1. The engine re-observes and scans from the last step backwards for a checkpoint that already holds.
     If one does, it skips ahead.
  2. Otherwise, if the current step's precondition holds and its target resolves, it re-runs that step.
  3. Otherwise it fails with `unrecognized_state`.

  In discovery the model re-observes, and the human's actions become `performed_by: human` steps flagged
  for review.
- **Designed but not built:** a remote isolated browser streamed to the console; blocking input while
  automation holds the lock (today the lock is cooperative); routing by tenant and skill; SLAs and
  notifications; console authentication.

## Safety

- **Default-deny allowlist** of origins, routes and actions. Each action is checked against both its
  destination (a URL or link `href`) and the document being acted on. The harness `/__admin` routes are
  unreachable to automation.
- **Risk classification is policy's job, never the model's.** An artifact can raise a step's risk, never
  lower it.
- **Irreversible actions** need operator approval in discovery, so the write flow can still be recorded.
  In replay they need an *approved* artifact, an explicit `--allow-irreversible`, and an allowlisted
  capability. Missing only the flag gives `NeedsHuman`, never a silent run. They are never auto-retried.
  I rejected blocking outright, because the flow then can't be captured, and flag-only, which is too weak
  for money movement.
- **Credentials** are references, resolved from the environment at use and registered with the redactor
  immediately. Sign-on is the profile's authored procedure, so credentials never enter a prompt.
- **Redaction** is one module covering every log, request, snapshot and result.
  - It scrubs known values: secrets, PII inputs, and values *observed* in sensitive fields, learned from
    screen labels and columns.
  - Field rules mask structured observations and, through Playwright masks, the screenshots inside
    frames. Regexes are a backstop.
  - The model is additionally kept from fields no task needs (SSN, DOB, phone, address), in both the
    outline and the screenshot.
  - Tests grep all output for planted secrets and PII.
- **Limits.**
  - The model sees names, balances and anything the field rules don't cover, and that data goes to the
    model provider.
  - Value- and field-based redaction can miss novel PII in free text.
  - Traces contain unredacted DOM. They're kept in a gitignored `restricted/` folder; production needs
    encryption and retention rules.
  - The lock is cooperative, the console is unauthenticated, and rate counting isn't tamper-proof.
  - Page text is treated as data and the gate is the backstop, but prompt injection is mitigated, not
    solved.

## Cuts

- **No live LLM run.** There was no API key in the build environment. The Anthropic client uses the
  current API shapes (`tool_choice: auto` with no parallel calls, strict tools, an append-only history,
  refusal fallbacks) and is tested against a fake SDK client. All recorded runs use MockLLM behind the
  same interface.
- **One surface.** Desktop and OCR-based surfaces are designed only.
- **Minimal console:** polling, no authentication, no queue, no streamed co-browsing.
- **No assisted single-step LLM recovery.** It's the natural next step after drift signals.
- **Human steps recorded in discovery are flagged, not locator-verified.**
- **Simplifications:** a restart from step 0 after re-auth (correct because of the probe), one probe
  type, file storage, naive rate limits. No stability scoring and no agent-facing catalog; the contract
  maps directly onto a tool definition.
- **Next, in order:** live-model evaluation across goals and tenants; assisted step recovery; canary
  replays with a drift dashboard; isolated remote browsers with an authenticated console; a UIA desktop
  prototype; an encrypted trace store.

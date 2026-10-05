# Decisions log

Running log of significant decisions: what, alternatives, why. Newest last.

## Phase 0

**D1. Python 3.12, pip + `pyproject.toml` (uv-compatible).**
Alternatives: system Python 3.14, uv. 3.14 is too new to trust every wheel
(greenlet, pydantic-core); 3.12 is installed via Homebrew. uv isn't installed; a
standard `pyproject.toml` works with both, so nobody is forced to install anything.

**D2. Default model `claude-opus-5-5`, overridable via `ANTHROPIC_MODEL`.**
It is the current default Claude model. Discovery runs once per capability, so its
cost is amortized over every replay; reliability of discovery matters more than
per-token price. Sonnet 5.5 is a reasonable override for cost.

**D3. Custom typed tools over accessibility refs, not Anthropic's built-in computer-use tool.**
Alternative: the `computer_toolset` (screenshot + coordinates). Coordinates are
what a human clicked, not *what* they clicked; replay needs semantic targets
(role/name, label, table cell) to be robust and reviewable. So the LLM picks an
element ref from an accessibility-style outline, and the recorder converts that ref
into several verified locators. A screenshot still goes to the model for visual
context. Coordinates remain the documented fallback for surfaces with no
accessibility tree.

**D4. No forced `tool_choice`.** Opus 5.5 rejects `any`/`tool`. Use `auto` +
`disable_parallel_tool_use` + `strict` tool schemas + an instruction that every turn
must be exactly one tool call. A text-only reply is nudged once and counts toward
no-progress. This also gives "exactly one action per observation" for free.

**D5. Exception handlers come from a per-vendor app profile, copied into the artifact at record time.**
Alternatives: have the LLM discover them (it only sees the happy path), or keep
them outside the artifact (then the artifact isn't self-contained or versioned).
The profile is vendor-product knowledge shared by every tenant running that
product, which is the multi-tenant lever.

**D6. Permission denied is a business outcome, not a failure or escalation.**
It's deterministic, a retry can't fix it, and it's not a bug. The calling agent
needs to tell its user "you're not allowed to see this member." Escalation remains
available via a handler override if a tenant wants a supervisor in the loop.

**D7. Validation errors from the app are business outcomes, with per-field details.**
Malformed params are caught *before* the UI (`Failure(invalid_input)`, a caller
bug). An app-side rejection (e.g. deposit over limit) is a legitimate answer the
caller can act on.

**D8. Session expiry: re-authenticate from a credential reference (≤1×), then restart the flow from step 0.**
Alternative: resume at the last checkpoint. Restarting is simpler and provably
correct for read flows. For the write flow, the idempotency probe makes a restart
safe. Policy can turn re-auth off, in which case the run escalates.

**D9. Irreversible actions: approval during discovery; approved artifact + explicit `--allow-irreversible` during replay.**
Alternatives: block outright (then the write flow can never be recorded), or merely
flag (not conservative enough for money movement). Risk is classified by policy
rules, never by the LLM, and an artifact can't downgrade it. Irreversible steps
are never auto-retried; ambiguity after submit goes to the idempotency probe, then
to a human.

**D10. Intervention state as JSON files, one writer per file.**
Alternative: SQLite. The runner writes `request.json`, the operator writes
`decision.json`, so there are no concurrent writers and no locking. The operator UI
can be a separate process. A real deployment would use a DB/queue; that's scaling
infrastructure the brief says not to build.

**D11. Fencing epoch on the control lock.**
A held lock alone doesn't stop a stale automation action that started before the
handoff. Each acquisition bumps an epoch, and the surface rejects acts carrying an
old one. It costs one integer.

**D12. Same-session proof via CDP.**
The browser runs headed with a CDP endpoint. A real human uses the window. The
non-interactive demo's scripted operator attaches to the same browser over CDP
from a separate process. A fresh browser would fail the brief's "not a fresh one"
requirement.

**D13. Playwright traces are restricted evidence, not committed evidence.**
Conflict: the prompt wants a trace for every run *and* redaction of all evidence,
but traces embed raw DOM snapshots that can't be reliably redacted. Traces go to a
gitignored `runs/<id>/restricted/` folder. Committed `evidence/` holds redacted
JSONL, masked screenshots and redacted accessibility snapshots.

**D14. Policy block during discovery stops the run (`StopReason.POLICY_BLOCKED`).**
Alternative: feed the refusal back to the LLM and let it try something else. An
attempt to leave the allowlist means the agent is confused or the page is
injecting instructions; either way, stopping is the safer default.
`requires_approval` is not a block; it escalates.

**D15. Member-not-found is exercised with a non-existent ID, not only an injection toggle.**
That is how it happens in reality. The injection toggle exists too, for parity.
One extra injectable condition, an unknown modal with no handler, drives the
"stuck replay → human" demo.

## Implementation (Phases 1–7)

**D16. The model never signs on.** Sign-on is an authored procedure in the vendor app profile, using
credential references, and is shared by discovery, replay and re-authentication. Alternative: let the LLM
type secrets by reference. Rejected because it puts the login page, and the risk of a mistyped secret,
inside the model loop for no benefit, and every capability on the product would re-learn the same login.

**D17. Three escalation modes: `fail`, `detach` (replay default) and `wait` (discovery default).**
An agent calling replay unattended gets `NeedsHuman` back, with a persisted request (screenshot, events,
reason) it can route. `wait` keeps the live session open for an operator. `fail` is for CI-style runs.
Without a session host that outlives the call, `detach` can't keep the browser alive. That is documented
as a cut.

**D18. "Artifact doesn't fit the UI" is a Failure; "UI is in an unknown state" is NeedsHuman.**
A missing or ambiguous target means the recording or an override must change, and a human taking over the
browser can't fix that. A checkpoint that times out with no known detector means the app is in a state an
operator can resolve.

**D19. Locator priority isn't fixed.** The default is role+name, label, adjacent label, text, table, CSS.
Row-scoped controls ("View" in a results row) put the table anchor first, and data cells never get a text
anchor. A lower-priority match is accepted only after an 800 ms grace period, so a half-loaded page can't
make us pick the CSS fallback. A lower-priority match is reported as drift.

**D20. A first row counts as a table header only if its cells are `<th>` or bold.** Otherwise key/value
tables ("Name: | Dana") were treated as data tables, with values as column headers. Found while testing
against the confirmation page.

**D21. The "unknown modal" is a blocking page, not an overlay.** An overlay doesn't block DOM reads, so
replay would correctly extract the balance anyway and never get stuck. A blocking page is the realistic
case where a human decision is needed.

**D22. Typed values are masked in logs as soon as they're typed (partial `•••42`).** The model classifies
inputs only at `finish`, after several log lines are already written. Over-redacting typed values is
cheaper than rewriting logs. Separately, a leak check refuses to save an artifact containing any value
known to be sensitive.

**D23. Two redaction levels.** `log` hides every sensitive field. `llm` hides only fields no task needs
(SSN, DOB, phone, address) from the model's outline and screenshot. This is data minimisation toward the
model provider without blinding the agent to what the goal asks for.

**D24. Checkpoints are derived, not declared by the model.** After each action the recorder takes the
route of the frame that navigated plus the first new emphasized text in that frame, matched exactly. For
typing it uses the field value. At replay, a checkpoint that was already true before a click counts only
after a navigation (a nav-counter guard). The model's own claims are never trusted as checkpoints.

**D25. A handler applies only to steps whose resulting page can show it.** These are matched by the
routes the step navigated to, not the route it started on. That keeps "member not found" off the typing
step.

**D26. The 5xx retry reloads the failed frame (an F5) with backoff, at most 2 times, and never on an
irreversible step.** If no frame errored, it re-runs the step.

**D27. The Phase 7 pick is cross-tenant reuse with tenant overrides, not the capability catalog.** It
exercises a core evaluation axis (generalisation) and the drift-detection story. The catalog would mostly
re-serialize the contract.

**D28. The idempotency probe lives in the vendor profile, keyed by a caller-supplied business key
(nickname).** Legacy apps don't expose idempotency keys. When the probe finds the effect already exists,
outputs are absent and `already_existed=true`, so outputs on write capabilities are `required: false`.

**D29. Anthropic client defaults.** `claude-opus-5-5` at `effort: medium`; `tool_choice: auto` with no
parallel tool calls and strict schemas (forced tool choice is rejected by this model); server-side refusal
fallbacks on (`CUA_LLM_FALLBACKS=0` turns them off); an append-only history so thinking blocks stay valid.

**D30. A version probe gates replay.** Replay refuses versions outside `app_version_range` rather than
guess. Tenant B runs 4.4.0, inside `>=4.0,<5.0`.

**D31. Mock app injection presets replace the current conditions; they don't merge.** Found while
testing: a sticky `not_found` from one run silently poisoned every later run. The harness now sets exactly
one condition per run and clears it afterwards.

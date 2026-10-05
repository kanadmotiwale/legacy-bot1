# cua: computer-use automation for legacy bank back-offices

**The model discovers. The artifact becomes a reusable capability. Deterministic replay is how an agent invokes it.**

An LLM drives a deliberately hostile legacy web app (frameset, nested iframes, table layouts, no test ids,
unlabeled inputs) to accomplish a natural-language goal. The successful run is recorded as a **typed,
versioned capability artifact** (inputs, outputs, business outcomes, multi-strategy locators, checkpoints,
error handlers). That artifact is then **replayed deterministically with no LLM**. Replay returns one of
four typed results, and when it gets stuck it can **hand the live browser session to a human** and take it
back. Everything is policy-gated and redacted.

The design is written up in [REPORT.md](REPORT.md). The decision log is [DECISIONS.md](DECISIONS.md), and
the demo output is in [evidence/SUMMARY.md](evidence/SUMMARY.md).

## Setup

Requires Python 3.11–3.13 (developed on 3.12) and macOS or Linux.
 
```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m playwright install chromium
cp .env.example .env        # optional: only needed for live LLM discovery
```

Configuration (all optional for the offline demo):

| Variable | Used for | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | live discovery with Claude | unset, so use `--mock-llm` |
| `ANTHROPIC_MODEL` | discovery model | `claude-opus-5-5` |
| `CUA_LLM_FALLBACKS` | `0` disables server-side refusal fallbacks | `1` |
| `MOCKBANK_USER` / `MOCKBANK_PASSWORD` | fake credentials for the synthetic app, resolved through credential references and never stored | see `.env.example` |

The guardrail policy is in [`config/policy.yaml`](config/policy.yaml). The vendor app profile (sign-on
procedure, error signatures, idempotency probe) is in [`config/apps/mockcore.yaml`](config/apps/mockcore.yaml),
and tenant overrides are in [`config/tenants/`](config/tenants/).

## Run everything offline (no API key, no network)

```bash
cua demo
```

This starts the mock bank in-process, runs discovery with the scripted **MockLLM**, and replays the
artifact through every error class. It also runs both human handoffs, using a scripted operator that
attaches to the live browser over CDP from a separate process, plus the write flow with approval and
idempotency, and cross-tenant reuse. It writes [`evidence/`](evidence/) and exits non-zero if any
scenario deviates from its expected result. It takes about 90 seconds.

MockLLM is not a recording. Each scripted intent describes its target semantically (role, label, column)
and is resolved against the live observation, the same way the real model picks element refs. It sits
behind the same `LLMClient` interface, so the recorder, the policy gate and replay can't tell the
difference.

## Demo path: discover, then replay

```bash
# terminal 1: the synthetic legacy app (http://localhost:8000, fake credentials in .env.example)
cua target-app

# terminal 2: discover (use --mock-llm without an API key; add --headed to watch)
cua discover --goal "Look up member 10042 and read their current savings balance" \
             --target http://localhost:8000 --mock-llm
#   -> artifacts/mockcore.member.read_savings_balance/1.0.0.json (draft)

# replay deterministically: different member, no LLM
cua replay artifacts/mockcore.member.read_savings_balance/1.0.0.json -p member_id=10077 --reveal

# the same replay under runtime conditions
cua replay artifacts/mockcore.member.read_savings_balance/1.0.0.json -p member_id=99999          # business outcome
cua replay artifacts/mockcore.member.read_savings_balance/1.0.0.json -p member_id=10042 --inject interstitial
cua replay artifacts/mockcore.member.read_savings_balance/1.0.0.json -p member_id=10042 --inject session_expiry
cua replay artifacts/mockcore.member.read_savings_balance/1.0.0.json -p member_id=10042 --inject 500_persistent
#   --inject: not_found | validation | permission | interstitial | session_expiry | slow | 500 | 500_persistent | unknown_modal
```

### Human handoff, live

```bash
cua operator                     # terminal 3: operator console at http://localhost:8001
cua replay artifacts/mockcore.member.read_savings_balance/1.0.0.json -p member_id=10042 \
    --inject unknown_modal --escalation wait --headed
```

Replay pauses at the unknown "Supervisor Override Required" page and files an intervention request. In
the console, click **Take control**. Then click **Acknowledge** in the headed browser window: this is the
same session, not a new one, and your click is recorded. Click **Resume** in the console. Replay
re-verifies where the app actually is and finishes. `cua decide <request-dir> take_control|approve|resume|abort`
does the same from the CLI.

### Write flow (irreversible) and approval

```bash
cua discover --mock-llm --goal "Open a new Sub-Savings sub-account nicknamed 'Rainy Day' for member 10042 \
  with an initial deposit of 25.00 from S01, and reach the confirmation screen"
# discovery pauses at Submit: approve it in the operator console (or `cua decide ... approve`)
cua approve artifacts/mockcore.subaccount.open/1.0.0.json --reviewer you
cua replay artifacts/mockcore.subaccount.open/1.0.0.json -p member_id=10042 -p account_type=Sub-Savings \
  -p nickname="Vacation Fund" -p initial_deposit=25.00 -p funding_account=S01 --allow-irreversible
```

### Another tenant, same artifact

```bash
cua override artifacts/mockcore.member.read_savings_balance/1.0.0.json --tenant lakeshore \
    --file config/tenants/lakeshore.yaml            # -> 1.1.0
cua replay artifacts/mockcore.member.read_savings_balance/1.1.0.json -p member_id=10042 --tenant lakeshore
```

### Live discovery with Claude

```bash
export ANTHROPIC_API_KEY=...            # or put it in .env
cua discover --goal "Look up member 10042 and read their current savings balance" --target http://localhost:8000 --headed
```

The live path was not exercised while building this repo, because there was no API key in the build
environment. It is the same loop as MockLLM, behind the same `LLMClient` interface. See `## Cuts` in
REPORT.md.

## Tests

```bash
pytest -q          # 66 tests, ~2 min, no API key needed (browser tests start the mock app on :8765)
pytest -q -m "not browser"   # unit tests only, < 1 s
```

They cover:
- schema validation, round-trip and versioning
- parameterization
- locator ordering, uniqueness, fallback and drift
- every injected condition against the live app
- the structural "replay never imports the LLM" guarantee
- policy decisions
- control-lock transitions, including illegal ones and stale fencing tokens
- same-session takeover through a separate CDP process
- a grep of everything written to disk for planted secrets and PII

## Layout

```
src/cua/
  surface/    Surface protocol, PlaywrightWebSurface, in-page dom.js, browser lifecycle (CDP endpoint)
  agent/      discovery loop, Anthropic client, MockLLM, tool schemas
  artifact/   schema (Pydantic), recorder, locator generation, params, binding (tenants), store, app profiles
  replay/     deterministic engine, locator resolution, result union
  policy/     PolicyGate, credential references, redaction (the single module)
  handoff/    control lock + fencing, intervention requests, coordinator, capture.js, scripted operator
  evidence/   JSONL run log, snapshots
  cli.py, demo.py
target_app/   the synthetic legacy bank (two tenants, injectable conditions)
operator_ui/  minimal operator console
schemas/      capability.schema.json (exported)
evidence/     demo output: artifacts, run logs, snapshots, interventions
```

All data is synthetic. SSNs use the never-issued 900 range.

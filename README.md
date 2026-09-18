# GridWise LLM — Smart Campus Energy Optimizer

An HTTP API that reads a 24-hour campus energy forecast (demand, solar, grid tariff) plus 1–3 natural-language operator notes, interprets the notes with an LLM into structured directives, and returns a cost-minimal 24-hour battery/grid schedule that obeys those directives.

Built for the **BUP CSE Fest 2026 Hackathon — Preliminary Round** ("GridWise" / LLM-Assisted Operator Directive Interpretation challenge).

## Live deployment

- **Base URL:** [`https://gridwise-llm-d21u.onrender.com`](https://gridwise-llm-d21u.onrender.com)
- **Health:** `GET https://gridwise-llm-d21u.onrender.com/health`
- **Main endpoint:** `POST https://gridwise-llm-d21u.onrender.com/optimize-energy`

No login, VPN, or manual approval required — both endpoints are directly reachable.

Deployed on [Render](https://render.com) directly from this repository's `Dockerfile` — the live URL above runs the exact same container image described in [Running with Docker](#running-with-docker), not a separate build. A scheduled job pings `/health` every 13 minutes to keep the instance warm and avoid Render free-tier cold starts.

---

## Table of Contents

- [Live deployment](#live-deployment)
- [How it works](#how-it-works)
- [Tech stack](#tech-stack)
- [Repository layout](#repository-layout)
- [Prerequisites](#prerequisites)
- [Getting a Gemini API key](#getting-a-gemini-api-key)
- [Setup & running locally](#setup--running-locally)
- [Running with Docker](#running-with-docker)
- [Environment variables](#environment-variables)
- [API reference](#api-reference)
- [Supported operator directives](#supported-operator-directives)
- [Testing](#testing)
- [Manual testing (curl / Swagger / Postman)](#manual-testing-curl--swagger--postman)
- [Design notes & safety behavior](#design-notes--safety-behavior)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Credits & dependencies](#credits--dependencies)

---

## How it works

```
POST /optimize-energy
        │
        ▼
FastAPI + Pydantic request validation  (app/schemas.py)
        │
        ▼
LLM directive interpretation           (app/llm_parser.py)
  — Gemini reads each operator note and returns one structured
    directive per note, using forced JSON schema output.
  — If the API key is missing, the call errors, or the model is
    unreachable, this step safely falls back to "no_op" for every
    note instead of crashing.
        │
        ▼
Deterministic guardrails               (app/guardrails.py)
  — The LLM's output is untrusted. This layer re-validates it:
    clamps factors/reserves to legal ranges, sorts/dedupes hours,
    forces exactly one entry per note, and demotes anything
    malformed or unsupported to a safe no_op.
        │
        ▼
Linear-programming optimizer           (app/optimizer.py)
  — Builds a 24-hour LP (PuLP + CBC solver): grid import, solar
    use, battery charge/discharge, respecting every validated
    directive, battery physics, and end-of-day neutrality.
  — Minimizes total grid electricity cost.
  — If the directives make the problem infeasible, it retries
    with directives disabled rather than returning an error.
        │
        ▼
JSON response: interpretation + 24-hour schedule + cost summary
```

The core idea (per the challenge spec): **human notes are never trusted directly as math.** They're converted to a fixed structured format by the LLM, checked by deterministic guardrails, and only then applied to the optimization model.

## Tech stack

| Layer | Choice |
|---|---|
| Language / runtime | Python 3.14 |
| Web framework | FastAPI + Uvicorn |
| Validation | Pydantic v2 |
| LLM | Google Gemini (`google-genai` SDK, async client, forced structured JSON output) |
| Optimization | PuLP (CBC solver) |
| Container | Docker (Debian slim, non-root, port 8000) |

## Repository layout

```
gridwise-llm/
├── app/
│   ├── main.py            # FastAPI app: GET /health, POST /optimize-energy
│   ├── schemas.py          # Pydantic models for request/response + validation rules
│   ├── llm_parser.py       # Gemini async client — note interpretation, safe fallback
│   ├── guardrails.py       # Deterministic sanitization of LLM output
│   └── optimizer.py        # PuLP linear program + post-processing
├── tests/
│   └── test_sample_01.py   # Automated tests against the public sample case pack
├── docs/
│   ├── preli.md                                    # Organizer's canonical problem statement
│   ├── gridwise_llm_project_spec.md                 # Internal technical blueprint
│   ├── BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json  # 10 worked test cases
│   └── phases.md                                    # Build plan this repo followed
├── requirements.txt
├── Dockerfile
├── .env.example
└── README.md
```

## Prerequisites

- Python 3.14 (or any modern Python 3.11+; 3.14 is what this repo was built and tested with)
- A free Google Gemini API key (see below)
- Docker Desktop — only needed if you want to build/run the container, not for local development

## Getting a Gemini API key

1. Go to **[Google AI Studio](https://aistudio.google.com)**.
2. Sign in with any Google account.
3. In the left sidebar, click **Get API key** → **Create API key**.
4. Copy the key (starts with `AIza...`).

This uses Gemini's free tier — no credit card required, generous daily request quota.

## Setup & running locally

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure your API key
cp .env.example .env
# then edit .env and paste your real GEMINI_API_KEY

# 4. Run the server
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

The server is now live at `http://localhost:8000`. Interactive API docs (Swagger UI) are auto-generated at `http://localhost:8000/docs`.

## Running with Docker

### Option A — pull the published fallback image (recommended for judges)

```bash
docker pull asifavaas/gridwise-llm:v1
docker run --rm -p 8000:8000 --env-file .env asifavaas/gridwise-llm:v1
curl http://localhost:8000/health
```

- **Registry:** Docker Hub
- **Image:** `asifavaas/gridwise-llm:v1`
- **Digest:** `sha256:ec8b5c6c16e0a605a2ff5e9020c514fd56aca06a2327ca0be41cd73ebc048e81`

Verified end-to-end from a clean pull (image removed locally, re-pulled fresh): `/health` returns `200` within seconds, `/optimize-energy` returns a correct, cost-optimal response, the container runs as the non-root `appuser`, and `docker history` confirms no `.env` file or secret value is baked into any layer — `GEMINI_API_KEY` must be supplied at runtime via `--env-file`/environment variables, exactly as with a local build.

### Option B — build from source

```bash
docker build -t gridwise-llm .
docker run --rm -p 8000:8000 --env-file .env gridwise-llm
```

Either way, the container installs the `coinor-cbc` solver at build time, declares a `HEALTHCHECK` against `/health`, and listens on `0.0.0.0:8000` — matching the judge harness's expected deployment shape. This exact `Dockerfile` is also what [powers the live deployment](#live-deployment) on Render.

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `GEMINI_API_KEY` | Yes (for real LLM calls) | — | Your Gemini API key. If unset, every operator note is safely interpreted as `no_op` instead of the service crashing. |
| `GEMINI_MODEL` | No | `gemini-flash-lite-latest` | Which Gemini model to call. The default is an alias that always tracks Google's current stable lite model, so it won't go stale as model versions rotate. |

Never commit `.env` — it's already excluded via `.gitignore`. Only `.env.example` (with placeholder values) is tracked.

## API reference

### `GET /health`

Readiness check.

```json
{ "status": "ok" }
```

### `POST /optimize-energy`

**Request body:**

```json
{
  "scenario_id": "SAMPLE-01",
  "operator_notes": [
    "Facilities will wash the rooftop solar panels from noon until 2 PM. During cleaning, usable solar should be treated as roughly 25% of the forecast.",
    "The sports office moved next month's registration deadline."
  ],
  "hours": [
    { "hour": 0, "demand_kwh": 90.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 6.0 }
    /* ... exactly 24 entries, one per hour 0–23 ... */
  ],
  "battery": {
    "capacity_kwh": 220.0,
    "initial_energy_kwh": 110.0,
    "minimum_energy_kwh": 40.0,
    "max_charge_kwh_per_hour": 50.0,
    "max_discharge_kwh_per_hour": 50.0
  }
}
```

- `operator_notes`: 1 to 3 non-empty natural-language strings.
- `hours`: exactly 24 entries covering hours 0–23 (any order in the array; each hour must appear exactly once).

**Response body:**

```json
{
  "scenario_id": "SAMPLE-01",
  "directive_interpretation": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": { "hours": [12, 13], "factor": 0.25 },
      "explanation": "Solar availability is reduced to 25% during the panel-cleaning window."
    },
    {
      "note_index": 1,
      "applies": false,
      "directive_type": "no_op",
      "structured_adjustment": null,
      "explanation": "This note does not affect today's 24-hour energy schedule."
    }
  ],
  "hourly_plan": [
    {
      "hour": 0,
      "grid_kwh": 90.0,
      "solar_used_kwh": 0.0,
      "battery_action": "idle",
      "battery_kwh": 0.0,
      "battery_energy_after_kwh": 110.0
    }
    /* ... 24 entries total ... */
  ],
  "total_grid_kwh": 2692.5,
  "total_cost_bdt": 38365.0,
  "peak_grid_kwh": 175.0,
  "plan_summary": "Uses the reduced midday solar availability, ignores the unrelated note, and shifts battery energy toward higher-tariff hours while restoring the initial battery level."
}
```

**HTTP status codes:**

| Code | When |
|---|---|
| `200` | Successful health check or optimization. |
| `400` | Malformed JSON or a structurally invalid request (missing/wrong-typed fields, wrong hour count, etc.). |
| `500` | Controlled internal error — never leaks a stack trace or secrets; logged server-side only. |

## Supported operator directives

Every operator note is converted into exactly one of the following, in `note_index` order:

| Directive type | Meaning | `structured_adjustment` shape |
|---|---|---|
| `solar_reduction` | Reduce usable solar during specific hours. | `{"hours": [...], "factor": number}` — factor is the *usable fraction remaining* (an 80% reduction → `factor: 0.2`) |
| `minimum_battery_reserve` | Keep battery energy at or above a required level during specific hours. | `{"hours": [...], "minimum_energy_kwh": number}` |
| `no_charge_window` | Battery charging is unavailable during specific hours. | `{"hours": [...]}` |
| `no_discharge_window` | Battery discharging is unavailable during specific hours. | `{"hours": [...]}` |
| `max_grid_window` | Grid import may not exceed a stated amount during specific hours. | `{"hours": [...], "max_grid_kwh": number}` |
| `no_op` | The note doesn't affect the schedule (distractor / irrelevant). | `null` |

Time windows are start-inclusive, end-exclusive: "1 PM to 3 PM" → `hours: [13, 14]`.

## Testing

```bash
pytest
```

Runs `tests/test_sample_01.py`, which checks:
- `/health` returns `200` with `{"status": "ok"}`.
- The full pipeline (LLM boundary mocked with ground-truth data, so this runs without a real API key) against `SAMPLE-01` from the public case pack: directive interpretation matches exactly, energy balance holds every hour, end-of-day battery neutrality holds, and reported totals are self-consistent with the returned schedule.
- The LLM fallback path safely returns `no_op` for every note when no API key is present.

To validate against **all 10 public sample cases** using a **real** Gemini key (not mocked), run:

```bash
python -c "
import asyncio, json
from app.llm_parser import interpret_notes
from app.guardrails import sanitize_directives
from app.optimizer import solve_schedule
from app.schemas import ScenarioRequest

data = json.load(open('docs/BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json', encoding='utf-8'))

async def main():
    for case in data['cases']:
        req = ScenarioRequest(**case['input'])
        raw = await interpret_notes(req.operator_notes, req.battery)
        directives = sanitize_directives(raw, req.battery, len(req.operator_notes))
        result = solve_schedule(req, directives)
        print(case['id'], result['total_cost_bdt'], 'vs expected', case['expected_output']['total_cost_bdt'])

asyncio.run(main())
"
```

## Manual testing (curl / Swagger / Postman)

**Easiest — Swagger UI:** with the server running, open `http://localhost:8000/docs` in a browser, expand `POST /optimize-energy`, click "Try it out", paste in one of the `input` objects from `docs/BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json`, and click Execute.

**curl:**
```bash
curl -X POST http://localhost:8000/optimize-energy \
  -H "Content-Type: application/json" \
  -d @sample_input.json
```

**Postman:** import the same JSON body as a raw POST to `http://localhost:8000/optimize-energy` with header `Content-Type: application/json`.

## Design notes & safety behavior

- **Stateless, no database.** Every request is self-contained; nothing is persisted between calls.
- **LLM output is never trusted directly.** `guardrails.py` re-validates every field the LLM returns — unsupported directive types, out-of-range hours, and out-of-bounds numeric values are all clamped or demoted to `no_op` rather than passed through.
- **Safe failure everywhere.** Missing API key, LLM timeout/error, malformed LLM output, or an infeasible optimization problem all degrade gracefully (to `no_op` directives, or to solving without directives) instead of returning `500` or crashing.
- **Battery neutrality is never relaxed.** Even in the infeasible-fallback path, the optimizer still enforces that the battery ends the 24-hour window at its starting energy level.
- **Equivalent optimal schedules are valid.** Because tariffs repeat across some hours, more than one hourly battery schedule can hit the same minimum cost — the judge (and these tests) check cost/constraint correctness, not a byte-for-byte match to one reference plan.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Every `directive_interpretation` entry comes back `no_op` even for relevant notes | `GEMINI_API_KEY` missing, invalid, or the Gemini call failed | Check `.env` has a real key; check server logs for the logged exception |
| `503 UNAVAILABLE` / "high demand" in logs | The specific Gemini model is temporarily overloaded | Try again, or set `GEMINI_MODEL` to a different available model (list them with `client.models.list()`) |
| `404 NOT_FOUND ... no longer available to new users` in logs | The pinned model id has been deprecated on Google's side | Switch `GEMINI_MODEL` to `gemini-flash-lite-latest` or another current model from `client.models.list()` |
| `cbc` solver not found (Docker) | `coinor-cbc` wasn't installed in the image | Rebuild the image — the `Dockerfile` installs it via `apt-get`; locally, PuLP's bundled CBC binary is used automatically as a fallback |
| `400` on every request | Request JSON doesn't match the schema (wrong hour count, missing field, etc.) | Compare your payload against the `docs/BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json` examples |

## Known limitations

- **No secondary objective on schedule shape.** The optimizer minimizes total cost only. When two or more hours share the same tariff, the LP has more than one optimal solution — the total cost and directive compliance are always correct, but the exact hour-by-hour battery action sequence (and therefore `peak_grid_kwh`) can differ from another equally-optimal schedule (including the public sample pack's reference plan). This is expected LP degeneracy, not a bug — see the challenge spec's "no byte-for-byte matching" clause.
- **One retry on LLM failure, then safe fallback.** If the Gemini call fails or returns unusable output (rate limit, transient 5xx, missing key), the service retries once after a 0.5s backoff; if that also fails, it falls back to `no_op` for every note in that request rather than erroring. The retry noticeably improves interpretation reliability against transient provider errors but adds a few seconds of tail latency on the occasional request that needs it (observed on the live deployment: most requests ~1.4-3s, occasional outliers ~8-12s when a retry fires) — still well within the 30s hard timeout.
- **Model availability depends on Google's rollout schedule.** `GEMINI_MODEL` defaults to the `gemini-flash-lite-latest` alias specifically to avoid pinning a model id that could be deprecated mid-event; if Google changes what that alias resolves to, interpretation quality could shift slightly.
- **No persistence, no caching, no rate limiting.** Every request is handled independently and statelessly; repeated identical requests each trigger a fresh LLM call rather than being cached.
- **No authentication.** The API is unauthenticated by design, matching the judge harness's requirement for direct, login-free access.
- **Render free-tier cold starts, mitigated.** Render's free tier spins an idle instance down after 15 minutes of inactivity, which would otherwise make the first request after a gap slow. A scheduled job pings `/health` every 13 minutes to keep the instance warm, so this shouldn't be hit during evaluation.
- **CBC solver, not a commercial LP solver.** For a 24-variable-per-hour LP this is fast and reliable, but CBC's tie-breaking behavior among equally optimal solutions is not customized (see the degeneracy point above).

## Credits & dependencies

This project's LLM → guardrails → optimizer architecture and all application code (`app/`) were designed and written specifically for this challenge. It builds on the following open-source libraries and external services:

| Dependency | Role |
|---|---|
| [FastAPI](https://fastapi.tiangolo.com/) | Web framework, request routing, OpenAPI/Swagger docs |
| [Uvicorn](https://www.uvicorn.org/) | ASGI server |
| [Pydantic v2](https://docs.pydantic.dev/) | Request/response schema validation |
| [PuLP](https://coin-or.github.io/pulp/) | Linear programming modeling layer |
| [CBC (COIN-OR)](https://github.com/coin-or/Cbc) | The actual LP solver PuLP calls |
| [google-genai](https://pypi.org/project/google-genai/) | Official Python SDK for the Google Gemini API |
| [Google Gemini API](https://ai.google.dev/) (`gemini-flash-lite-latest`) | The LLM used to interpret `operator_notes` into structured directives |
| [python-dotenv](https://pypi.org/project/python-dotenv/) | Loads `.env` for local development |
| [pytest](https://pytest.org/) | Test runner |

Development was assisted by an AI coding assistant (Claude Code); all architecture, prompt design, guardrail rules, and optimization formulation were reviewed and validated by the team against the official Problem Statement and Participant Guide.

---

See `docs/preli.md` for the organizer's canonical problem statement and `docs/gridwise_llm_project_spec.md` for the full internal technical blueprint.

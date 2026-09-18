# GridWise LLM — Build Phases

Source: docs/preli.md (canonical rules) + docs/gridwise_llm_project_spec.md (implementation blueprint).

## Phase 1 — Project Scaffold
- Create repo structure: `app/`, `tests/`, `requirements.txt`, `.env.example`, `Dockerfile`, `README.md`.
- Pin dependencies (FastAPI, Uvicorn, Pydantic v2, PuLP, anthropic, python-dotenv, httpx, pytest).
- No logic yet — empty modules only: `main.py`, `schemas.py`, `llm_parser.py`, `guardrails.py`, `optimizer.py`.

## Phase 2 — Schemas (Pydantic)
- `schemas.py`: `HourData`, `BatteryData`, `ScenarioRequest` (scenario_id, operator_notes[1-3], hours[24], battery).
- `DirectiveType` enum (6 types), `BatteryAction` enum.
- `DirectiveInterpretation`, `HourlyPlanItem`, `ScenarioResponse`.
- Validate: hours array exactly 24, unique 0-23; operator_notes 1-3 non-empty.

## Phase 3 — LLM Directive Parser
- `llm_parser.py`: AsyncAnthropic client, single tool-use call (`submit_interpretations`), forces one entry per note in note_index order.
- Prompt includes battery capacity (for percentage reserve math) and exact directive definitions from spec Section 04/4.1.
- Failure handling: API error/timeout/malformed → fallback to all `no_op` entries (never HTTP 500).

## Phase 4 — Guardrails (Deterministic Validation)
- `guardrails.py`: enforce length = N notes, note_index completeness/uniqueness.
- no_op ⇒ applies=false, structured_adjustment=null; non-no_op ⇒ applies=true.
- Hours: dedupe, clamp to [0,23], sort ascending.
- Clamp factor [0,1], minimum_energy_kwh [0,capacity_kwh], max_grid_kwh ≥ 0.
- Reject unsupported directive_type → treat as no_op (safe failure, never crash).

## Phase 5 — Optimizer (PuLP LP)
- `optimizer.py`: build LP per spec Section 7 — vars g_h, s_h, c_h, d_h, E_h.
- Constraints: energy balance, effective solar bound, battery transition (E_0 from initial, E_h chain), battery bounds (base + directive reserve), charge/discharge rate limits incl. no_charge/no_discharge windows, max_grid_window cap, end-of-day neutrality E_23 = initial.
- Objective: minimize sum(g_h * tariff_h).
- Solve via `pulp.COIN_CMD(path=shutil.which("cbc"), msg=False)`.
- Infeasible fallback: resolve with directives disabled (no_op) rather than failing — never relax battery neutrality.
- Post-process: battery_action discretization (ε=1e-4), round total_grid_kwh/total_cost_bdt/peak_grid_kwh to 2dp, hourly values to 4dp.

## Phase 6 — API Wiring (FastAPI)
- `main.py`: `GET /health` → `{"status":"ok"}`.
- `POST /optimize-energy`: parse request → llm_parser → guardrails → optimizer → ScenarioResponse.
- Exception handlers: malformed JSON/schema → 400/422; internal errors → controlled 500, no stack trace/secrets leak.
- Load `ANTHROPIC_API_KEY` from env only (python-dotenv), no hardcoded keys.

## Phase 7 — Testing & Local Validation
- `tests/test_sample_01.py`: run SAMPLE-01 scenario, assert directive interpretation matches expected (solar_reduction hours[12,13] factor 0.25; note 1 no_op).
- Assert energy balance holds all 24h, E_23 == initial_energy_kwh, metrics within 0.01 tolerance of reference (~2692.5 kWh, ~38365 BDT, ~175 peak).
- Manual checks against other directive types (no_charge_window, minimum_battery_reserve, max_grid_window) with ad-hoc payloads.

## Phase 8 — Containerize & Deploy Readiness
- Multi-stage Dockerfile (python:3.14-slim, installs `coinor-cbc`, non-root user, port 8000).
- Verify `docker build` + `docker run` → `/health` responds within 60s boot.
- README with local run + docker run instructions, `.env.example` filled with placeholder key.
- Final smoke test against Public Sample Cases JSON before submission.

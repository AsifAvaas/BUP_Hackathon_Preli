# GridWise Improvement Plan

Written for: the team, to execute against directly before submission.

## Status

**All 10 public sample cases pass, verified two independent ways** — live server hitting the real Gemini API, and the repo's own hermetic `tests/test_all_samples.py` with the LLM mocked to ground truth. Interpretation accuracy 18/18, schema correctness 110/110, GridWise/directive-application checks 1529/1529, cost-quality ratio 1.000 (exact reference-optimal cost on every case). The one real bug found in testing is fixed. Everything below is optional, ranked by points-per-minute against the rubric's own 100-point breakdown, aimed at converting "technically correct" into "clearly the strongest submission in the room."

---

## ✅ Fixed: optimizer battery-churn bug

**What it was:** the LP minimized grid cost but had nothing stopping the solver from charging and discharging the battery in the same hour by equal amounts whenever both were feasible (typically once the battery hit `capacity_kwh` or its floor mid-schedule). That's cost-neutral to the objective but physically meaningless, and `_build_hourly_plan` could only report one action per hour — so it silently dropped whichever value it didn't report, producing an `hourly_plan` row where `battery_action`/`battery_kwh` didn't reconcile with `battery_energy_after_kwh`. Hit SAMPLE-02, 04, and 08 (6 of 24 hours in SAMPLE-02 alone).

**Why it mattered:** the rubric treats a battery-transition mismatch as a hard invalidity — "no optimization credit" for the affected case (§09 Critical Violations), even when `total_cost_bdt` is exactly optimal. This was pure, avoidable loss across **Directive Application & Constraint Correctness (25 pts)** and **Optimization Quality (10 pts)**.

**What was applied** (`app/optimizer.py:101-124`): a binary `is_charging[h]` variable per hour with big-M constraints —

```python
prob += charge[h] <= charge_cap[h] * is_charging[h]
prob += discharge[h] <= discharge_cap[h] * (1 - is_charging[h])
```

This *structurally* forbids simultaneous charge and discharge rather than just discouraging it with a tie-breaking cost penalty — stricter and more robust than the epsilon-penalty approach first proposed, since it can't be defeated by a case where the LP happens not to care about the tiny penalty. Verified: re-ran both test paths, all 10 cases now pass, and cost is byte-identical to before the fix on the 6 cases that were already passing (confirms the fix changes nothing except the previously-degenerate hours).

---

## Priority order for remaining time

Ranked by rubric points at stake ÷ minutes to do it. Do them top to bottom until time runs out — nothing below this line is required to have a correct, passing submission.

| # | Item | Rubric category (pts) | Effort |
|---|---|---|---|
| 1 | [Docker fallback dry run](#1-docker-fallback-dry-run) | Deployment & Docker Fallback (10) | 15 min |
| 2 | [LLM retry before fallback](#2-llm-retry-before-fallback) | LLM Directive Interpretation (25) | 10 min |
| 3 | [Solver timeout guard](#3-solver-timeout-guard) | Performance & Reliability (10) | 10 min |
| 4 | [Paraphrase spot-check](#4-paraphrase-spot-check-no-code-change) | LLM Directive Interpretation (25) | 10 min |
| 5 | [Concurrent-load smoke test](#5-concurrent-load-smoke-test) | Performance & Reliability (10) | 10 min |
| 6 | [README/model-choice audit](#6-readmemodel-choice-audit) | Documentation (10) | 5 min |
| 7 | [3-minute video script](#7-3-minute-video-script) | Tie-break only, no base points | 20 min |

---

### 1. Docker fallback dry run

This is its own untested 10-point category — nobody has actually built and run the container from a clean state yet.

```bash
docker build -t gridwise-llm .
docker run --rm -p 8000:8000 --env-file .env gridwise-llm
curl http://localhost:8000/health
# then POST one case from docs/BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json
```

Confirm explicitly:
- Binds `0.0.0.0`, not `127.0.0.1` (the README already documents this correctly — just verify the running container actually does it).
- `/health` responds within 60s of container start (rubric-scored threshold).
- `docker history gridwise-llm` shows no `.env` layer — the Dockerfile must never `COPY .env` or bake in the key. This is a named penalty item, worth a direct check, not an assumption.
- The documented `docker run` command in the README is the exact one that works, copy-pasted, not a paraphrase of it.

Do this early — if the Dockerfile needs a fix, you want slack time left, not none.

### 2. LLM retry before fallback

`app/llm_parser.py` currently falls back to marking every note `no_op` on the *first* exception from Gemini. That's a safe failure (correct, never crashes), but one transient rate-limit or network blip during the 4-hour window currently zeroes the entire 25-point interpretation category for that request — for notes that were perfectly answerable.

Add one retry with a short backoff before giving up:

```python
import asyncio

async def interpret_notes(operator_notes: list[str], battery: BatteryData) -> list[dict]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY missing; falling back to no_op interpretations")
        return _fallback_no_op(operator_notes)

    client = genai.Client(api_key=api_key)
    for attempt in range(2):  # 1 initial + 1 retry
        try:
            response = await client.aio.models.generate_content(
                model=MODEL,
                contents=_build_user_message(operator_notes, battery),
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    response_schema=_RawResponse,
                ),
            )
            parsed: _RawResponse | None = response.parsed
            if parsed is not None and parsed.interpretations:
                return [item.model_dump() for item in parsed.interpretations]
            logger.warning("LLM response missing usable structured output (attempt %d)", attempt)
        except Exception as exc:
            logger.warning("LLM interpretation call failed (attempt %d): %s", attempt, exc)
            if attempt == 0:
                await asyncio.sleep(0.5)
    logger.exception("LLM interpretation exhausted retries; falling back to no_op")
    return _fallback_no_op(operator_notes)
```

One 500ms retry is well inside the 30s per-request timeout budget, and keeps the exact same safe-failure guarantee (never invents a directive, never crashes) — it just stops a single flaky call from being fatal to the whole category.

### 3. Solver timeout guard

CBC on a 24-hour, ~120-variable LP is fast (sub-second on a normal machine), but there's currently no upper bound on solve time if the deployment host is slow or under load, and no bound at all if a future scenario made the model larger. Since `Per-request timeout` is a hard 30s rubric threshold and p95 latency is separately scored, add an explicit solver time limit so a slow solve degrades predictably instead of eating the whole request budget:

```python
def _solver() -> pulp.LpSolver:
    cbc_path = shutil.which("cbc")
    if cbc_path:
        return pulp.COIN_CMD(path=cbc_path, msg=False, timeLimit=15)
    return pulp.PULP_CBC_CMD(msg=False, timeLimit=15)
```

15s leaves headroom under the 30s hard timeout for FastAPI overhead, JSON serialization, and network latency, while still comfortably exceeding CBC's actual solve time on this problem size. If it ever does time out, `prob.solve()` returns a non-`"Optimal"` status, which the existing infeasible-fallback path in `solve_schedule` already handles (retries without directives) — so this is a pure safety margin, not a new failure mode to build.

### 4. Paraphrase spot-check (no code change)

The rubric names "paraphrase robustness" as its own explicit 5-of-25-point sub-criterion and states hidden notes will reword the same directive differently from the public samples. Your only automated coverage right now is the 10 public cases' exact wording. Before the window closes, manually POST a few paraphrases that are *not* in the public set and confirm they still resolve correctly:

- *"Panels are getting cleaned between 1 and 3pm, expect roughly a quarter of normal output"* → `solar_reduction`, hours `[13,14]`, factor ≈ `0.25`
- *"We need 60 kWh minimum in the battery at all times from 6pm to 9pm"* → `minimum_battery_reserve`
- *"Please avoid pulling from the battery between 5 and 7 in the evening"* → `no_discharge_window`, hours `[17,18]`
- *"Substation can't take more than 150 kW from us between 6 and 8 tonight"* → `max_grid_window`

Ten minutes of manual poking against the live endpoint, no code involved — just confidence that the LLM path actually generalizes rather than having gotten lucky on the public wording.

### 5. Concurrent-load smoke test

The rubric's Performance & Reliability category scores "valid-request stability/failure rate" under repeated requests, not just a single happy-path call. A quick concurrency check catches issues (shared state, connection pool exhaustion, solver contention) that a single manual `curl` never will:

```bash
# fires 10 concurrent requests using a public sample case
seq 1 10 | xargs -P10 -I{} curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" \
  -X POST http://localhost:8000/optimize-energy \
  -H "Content-Type: application/json" -d @sample_input.json
```

Expect all `200`s and consistent latency (no request dramatically slower than the others, which would suggest lock contention or a shared resource being serialized unexpectedly). The service is stateless per the README's own design notes, so this should pass trivially — worth confirming rather than assuming, since it's cheap and this is exactly the kind of thing a judge's repeated hidden-test harness will exercise.

### 6. README/model-choice audit

The current README already documents the model/provider (`gemini-flash-lite-latest` via `google-genai`), required env vars, optimizer/solver (PuLP/CBC), sample request/response, and known limitations — this is in good shape. Two things worth a final pass:

- Confirm the "Known limitations" section (already present, already honest about LP degeneracy and the no-retry LLM path) gets updated once items #2 and #3 above are applied — an outdated limitations list reads worse than no limitations list, since judges are explicitly instructed to check documentation against the submitted artifacts.
- Confirm the manual "all 10 cases with a real key" snippet in the Testing section still matches the current `app/` module signatures after any of the above changes (it calls `interpret_notes`, `sanitize_directives`, `solve_schedule` directly) — a copy-pasted command that throws an import error on a judge's clean checkout is a completely avoidable Documentation-category deduction.

### 7. 3-minute video script

No base points, but it's the **first tie-break criterion** — and ties are common in a 100-point automated rubric where most competent teams converge on similar correctness. A tight, technical script beats a rambling one. Structure that maps directly to what reviewers are told to compare (Guide §06, §10):

1. **Problem in one sentence** (10s): "Interpret free-text operator notes into safe, verifiable energy-schedule constraints, then optimize cost without ever trusting the LLM's output directly."
2. **Architecture diagram walkthrough** (45s): the five-stage pipeline already in the README (Energy Data + Notes → LLM Interpreter → Guardrail Validator → Math Optimizer → Final Validator → API Response) — say each stage's *one job* out loud, don't just show the diagram.
3. **The one bug you found and fixed** (45s): this is a genuine differentiator — most teams won't have a debugging story this concrete. "We found the LP could charge and discharge the battery in the same hour for free, which passed the cost check but broke the physical consistency of the schedule. We fixed it by adding a binary decision variable that structurally forbids it, not just penalizing it — here's the before/after on our own test suite." Show the `tests/test_all_samples.py` PASS table if you can screen-record it.
4. **How you're confident about hidden cases** (30s): mention the paraphrase testing (#4) and that guardrails re-validate every LLM output field regardless of wording.
5. **Run it live or show the run** (30s): a real `curl`/Swagger call against the deployed endpoint, or Docker `run` from a clean pull — whichever you're most confident won't glitch on camera.

Keep it under 3:00 with margin — going over is a stated requirement, not a suggestion.

---

## What not to do

- Don't touch the optimizer's core LP structure again beyond the already-applied fix — it's now verified correct against all 10 cases and matches reference-optimal cost exactly. Further "optimization" of an already-optimal LP is wasted time.
- Don't add caching or persistence "for performance" — the README already documents this as a deliberate stateless design, and the rubric doesn't reward it; it only adds surface area for a bug days before a deadline.
- Don't loosen the guardrails to "trust the LLM more" in pursuit of speed — deterministic validation before optimization is an explicit, named requirement (Problem Statement §08), not an implementation detail you can trade away.

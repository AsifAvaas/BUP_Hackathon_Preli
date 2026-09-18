# Project Specification: GridWise LLM Smart Campus Energy Optimizer

This document serves as the canonical technical specification and code generation blueprint for building the **GridWise LLM** automated energy scheduling and operator directive interpretation service for the BUP CSE Fest 2026 Preliminary Round.   

## 1. Challenge Overview & System Objective

The system operates as a stateless HTTP microservice that receives a 24-hour campus energy profile (hourly electricity demand, base solar availability, and grid tariffs) along with 1 to 3 unstructured natural-language operator notes.   

The service must:

1. Parse and classify operator notes using an LLM into structured, machine-checkable directives or mark them as `no_op` distractors.   
2. Deterministically validate and sanitize the extracted directives through strict guardrails.   
3. Apply the validated directives to a Linear Programming (LP) model.   
4. Solve the 24-hour scheduling problem using PuLP (COIN-OR CBC solver) to minimize total grid electricity expenditure in BDT while respecting all physical, operational, and directive constraints.   
5. Return the verified directive interpretations and the hourly operating plan via a strict JSON response within the judging latency threshold (p95 $\le$ 5s, hard timeout 30s).   

## 2. Tech Stack & Architectural Decisions

- **Language & Runtime:** Python 3.14 or the latest version
- **Web Framework:** FastAPI + Uvicorn (async event loop)
- **Data Validation & Serialization:** Pydantic v2
- **Optimization Engine:** PuLP using system-installed CBC solver (via `pulp.COIN_CMD(path=shutil.which("cbc"), msg=False)`)
- **LLM Provider & SDK:** Anthropic Async Client (`anthropic.AsyncAnthropic`) utilizing Claude 3.5 Haiku (`claude-3-5-haiku-20241022`) via native Tool Use / Structured Outputs for sub-second parsing latency
- **Persistence / Database:** None (the challenge requires a strictly stateless HTTP API; state persistence violates scoring isolation rules)
- **Containerization:** Docker (Debian slim base, multi-stage build, non-root user, listening on `0.0.0.0:8000`)   

```
                       [ Incoming POST /optimize-energy ]
                                       │
                                       ▼
                   ┌───────────────────────────────────────┐
                   │  FastAPI / Pydantic Request Parsing   │
                   └───────────────────────────────────────┘
                                       │
                                       ▼
                   ┌───────────────────────────────────────┐
                   │  LLM Directive Parser (Single Call)   │
                   │  Claude 3.5 Haiku + Tool Use Schema   │
                   └───────────────────────────────────────┘
                                       │
                        (Safe Fallback if API fails)
                                       │
                                       ▼
                   ┌───────────────────────────────────────┐
                   │    Deterministic Guardrail Layer      │
                   │  - Ascending unique hours [0..23]     │
                   │  - Bounds clamping (factor, reserve)  │
                   │  - applies / structured_adj integrity │
                   └───────────────────────────────────────┘
                                       │
                                       ▼
                   ┌───────────────────────────────────────┐
                   │ PuLP Linear Program (CBC Solver)      │
                   │  - Energy balance per hour            │
                   │  - Battery limits & SoC neutrality    │
                   │  - Directive overrides                │
                   └───────────────────────────────────────┘
                                       │
                                       ▼
                   ┌───────────────────────────────────────┐
                   │ Post-Processor & Response Assembler   │
                   │  - Action mapping (charge/discharge)  │
                   │  - Summary recomputation & rounding   │
                   └───────────────────────────────────────┘
                                       │
                                       ▼
                      [ HTTP 200 Structured JSON Plan ]

```

## 3. Endpoints & API Contract

### 3.1. `GET /health`

- **Purpose:** System readiness check for the automated judging harness. Must respond within 60 seconds of container boot.   
- **Response Code:** `200 OK`

     
- **Payload:**

```json
{
  "status": "ok"
}
```

### 3.2. `POST /optimize-energy`

- **Purpose:** Core processing endpoint accepting scenario data and operator notes, returning directive interpretations and the optimal 24-hour schedule.   
- **Latency Requirement:** Aim for p95 $\le$ 5.0 seconds (hard cutoff at 30.0 seconds).   
- **Status Codes:**
  - `200 OK`: Valid execution (returns interpretation + optimized plan).   
  - `400 Bad Request`: Malformed JSON or structurally invalid payload.   
  - `422 Unprocessable Entity`: Semantic schema validation failure.   
  - `500 Internal Server Error`: Controlled internal failure (never leak stack traces or API keys).   

## 4. Request & Response Schemas

### 4.1. Request Schema

```json
{
  "scenario_id": "SAMPLE-01",
  "operator_notes": [
    "Facilities will wash the rooftop solar panels from noon until 2 PM. During cleaning, usable solar should be treated as roughly 25% of the forecast.",
    "The sports office moved next month's registration deadline."
  ],
  "hours": [
    {
      "hour": 0,
      "demand_kwh": 90.0,
      "solar_kwh": 0.0,
      "tariff_bdt_per_kwh": 6.0
    }
    // ... exactly 24 objects for hours 0 through 23
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

### 4.2. Response Schema

```json
{
  "scenario_id": "SAMPLE-01",
  "directive_interpretation": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": {
        "hours": [12, 13],
        "factor": 0.25
      },
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
    // ... exactly 24 objects for hours 0 through 23
  ],
  "total_grid_kwh": 2692.5,
  "total_cost_bdt": 38365.0,
  "peak_grid_kwh": 175.0,
  "plan_summary": "Uses the reduced midday solar availability, ignores the unrelated note, and shifts battery energy toward higher-tariff hours while restoring initial battery level."
}
```

## 5. Operator Directive Interpretation Engine

### 5.1. Supported Directive Types & JSON Structures

1. `solar_reduction`:
   - **Semantics:** Multiplies base available solar by `factor` during specified hours.   
   - **Rule:** `factor` is the **usable fraction remaining**. "80% reduction" $\rightarrow$ `factor = 0.20`; "drops to 25%" $\rightarrow$ `factor = 0.25`; "one-fifth output" $\rightarrow$ `factor = 0.20`.   
   - **Shape:** `{"hours": [int, ...], "factor": float}`

        
2. `minimum_battery_reserve`:
   - **Semantics:** Sets a floor for battery energy level: $E_{\text{after}}[h] \ge \max(E_{\text{min\_base}}, E_{\text{min\_directive}})$.   
   - **Percentage Handling:** If the note specifies a percentage (e.g., "Keep 50% capacity"), the engine calculates $\text{percentage} \times \text{capacity\_kwh}$ into an absolute kWh value.   
   - **Shape:** `{"hours": [int, ...], "minimum_energy_kwh": float}`

        
3. `no_charge_window`:
   - **Semantics:** Battery cannot charge during these hours.   
   - **Shape:** `{"hours": [int, ...]}`

        
4. `no_discharge_window`:
   - **Semantics:** Battery cannot discharge during these hours.   
   - **Shape:** `{"hours": [int, ...]}`

        
5. `max_grid_window`:
   - **Semantics:** Campus grid import cannot exceed `max_grid_kwh` during these hours.   
   - **Shape:** `{"hours": [int, ...], "max_grid_kwh": float}`

        
6. `no_op`:
   - **Semantics:** Note is an operational distractor or does not alter the energy schedule.   
   - **Shape:** Must set `applies: false`, `directive_type: "no_op"`, `structured_adjustment: null`.   

### 5.2. Time Window Formatting & Parsing Rules

- Time intervals are **start-inclusive and end-exclusive** ($[\text{start}, \text{end})$).   
  - "1 PM to 3 PM" / "13:00 to 15:00" $\rightarrow$ `[13, 14]`
  - "noon until 2 PM" $\rightarrow$ `[12, 13]`
  - "6 PM until 9 PM" $\rightarrow$ `[18, 19, 20]`

- All extracted `hours` arrays must contain unique integers from `0` to `23` sorted in ascending order.   

## 6. Deterministic Guardrails & Sanitization Logic

The LLM output is untrusted and must pass through programmatic sanitizers before being passed to PuLP:   

1. **Ordering & Completeness:**
   - Exactly $N$ entries for $N$ operator notes, indexed `0` to $N-1$ strictly matching the input order.   
2. **Flag Consistency:**
   - If `directive_type == "no_op"`: enforce `applies = False` and `structured_adjustment = None`.   
   - If `directive_type != "no_op"`: enforce `applies = True`.   
3. **Hours Sanitization:**
   - For every directive with `hours`, remove duplicates, filter out any integer outside $[0, 23]$, and sort in ascending order.   
4. **Value Clamping:**
   - `solar_reduction`: Clamp `factor` strictly between $0.0$ and $1.0$.   
   - `minimum_battery_reserve`: Clamp `minimum_energy_kwh` between `0.0` and `battery.capacity_kwh`.   
   - `max_grid_window`: Ensure `max_grid_kwh` $\ge 0.0$.   
5. **Controlled Fallback:**
   - If the LLM call times out, returns unparsable data, or encounters an Anthropic API error, the service must catch the exception and fall back to returning safe `no_op` entries for all notes rather than failing with an HTTP 500 error.   

## 7. Mathematical Optimization Formulation (PuLP)

### 7.1. Decision Variables (for $h = 0, \dots, 23$)

- $g_h \ge 0$: Grid electricity import at hour $h$ ($\text{kWh}$).   
- $s_h \ge 0$: Solar energy consumed at hour $h$ ($\text{kWh}$).   
- $c_h \ge 0$: Battery charge amount in hour $h$ ($\text{kWh}$).   
- $d_h \ge 0$: Battery discharge amount in hour $h$ ($\text{kWh}$).   
- $E_h \ge 0$: Battery state of charge immediately after hour $h$ ($\text{kWh}$).   

### 7.2. Objective Function

Minimize total campus electricity procurement cost across the 24-hour horizon:
&#x20;  

$$\min \sum_{h=0}^{23} \left( g_h \times \text{tariff\_bdt\_per\_kwh}[h] \right)$$

### 7.3. Operational & Physical Constraints

1. **Hourly Energy Balance:**

   $$g_h + s_h + d_h = \text{demand\_kwh}[h] + c_h \quad \forall h \in [0, 23]$$
      
2. **Solar Availability Bound:**

   $$0 \le s_h \le \text{effective\_solar\_kwh}[h] \quad \forall h \in [0, 23]$$

   Where $\text{effective\_solar\_kwh}[h] = \text{solar\_kwh}[h] \times \text{factor}_h$ if a `solar_reduction` directive is active on hour $h$, else $\text{solar\_kwh}[h]$.   

3. **Battery Dynamics & State Transition:**

   $$E_0 = \text{initial\_energy\_kwh} + c_0 - d_0$$
   $$E_h = E_{h-1} + c_h - d_h \quad \forall h \in [1, 23]$$
      
4. **Battery Energy Bounds:**

   $$\max\left(\text{minimum\_energy\_kwh}, \text{directive\_reserve}[h]\right) \le E_h \le \text{capacity\_kwh} \quad \forall h \in [0, 23]$$
      
5. **Battery Rate Limits:**

   $$c_h \le (0 \text{ if } h \in \text{no\_charge\_window} \text{ else } \text{max\_charge\_kwh\_per\_hour})$$
   $$d_h \le (0 \text{ if } h \in \text{no\_discharge\_window} \text{ else } \text{max\_discharge\_kwh\_per\_hour})$$
      
6. **Grid Import Ceiling:**

   $$g_h \le (\text{max\_grid\_kwh} \text{ if } h \in \text{max\_grid\_window} \text{ else } \infty)$$
      
7. **Daily Energy Neutrality:**

   $$E_{23} = \text{initial\_energy\_kwh}$$

   (The battery cannot be permanently depleted to purchase cheaper energy).   

## 8. Post-Processing & Output Generation

### 8.1. Battery Action Discretization

To prevent numerical solver noise from producing fractional actions, classify actions with an epsilon ($\epsilon = 10^{-4}$):

```python
if c_val > 1e-4:
    battery_action = "charge"
    battery_kwh = c_val
elif d_val > 1e-4:
    battery_action = "discharge"
    battery_kwh = d_val
else:
    battery_action = "idle"
    battery_kwh = 0.0
```

### 8.2. Recalculation & Summary Formatting

- `total_grid_kwh`: $\sum_{h=0}^{23} g_h$ rounded to 2 decimal places.   
- `total_cost_bdt`: $\sum_{h=0}^{23} (g_h \times \text{tariff}[h])$ rounded to 2 decimal places.   
- `peak_grid_kwh`: $\max_{h}(g_h)$ rounded to 2 decimal places.   
- **Tolerance Handling:** All solver solutions are evaluated within an absolute tolerance of $0.01\text{ kWh}$ or $0.01\text{ BDT}$. Leave individual hourly numbers at raw solver precision or round to 4 decimal places to prevent compounding drift in the judge replay harness.   

## 9. File-by-File Implementation Blueprint

When invoked with Claude Code, generate the repository with the exact file structure below:

```
gridwise-llm/
├── app/
│   ├── __init__.py
│   ├── main.py            # FastAPI entry point, /health, /optimize-energy
│   ├── schemas.py         # Pydantic models for request, response, and tools
│   ├── llm_parser.py      # AsyncAnthropic client with Tool Use schema
│   ├── guardrails.py      # Deterministic validation and sanitizer
│   └── optimizer.py       # PuLP LP formulation and post-processing
├── tests/
│   ├── __init__.py
│   └── test_sample_01.py  # Unit test running SAMPLE-01 case
├── Dockerfile             # Multi-stage container definition
├── requirements.txt       # Locked production dependencies
├── .env.example           # Environment template
└── README.md              # Documentation with execution instructions
```

### 9.1. `requirements.txt`

```plaintext
fastapi>=0.110.0,<0.111.0
uvicorn[standard]>=0.28.0,<0.29.0
pydantic>=2.6.4,<3.0.0
pulp>=2.8.0,<3.0.0
anthropic>=0.21.0,<1.0.0
python-dotenv>=1.0.1
httpx>=0.27.0
pytest>=8.1.0
```

### 9.2. `app/schemas.py`

Define exact Pydantic models matching Section 07 and Section 10 of the Problem Statement:

- `HourData`: `hour` (int 0..23), `demand_kwh` (float $\ge 0$), `solar_kwh` (float $\ge 0$), `tariff_bdt_per_kwh` (float $\ge 0$).   
- `BatteryData`: `capacity_kwh`, `initial_energy_kwh`, `minimum_energy_kwh`, `max_charge_kwh_per_hour`, `max_discharge_kwh_per_hour`.   
- `ScenarioRequest`: `scenario_id` (str), `operator_notes` (List[str], len 1..3), `hours` (List[HourData], len 24), `battery` (BatteryData).   
- `DirectiveType`: Enum (`solar_reduction`, `minimum_battery_reserve`, `no_charge_window`, `no_discharge_window`, `max_grid_window`, `no_op`).
- `BatteryAction`: Enum (`charge`, `discharge`, `idle`).   
- `StructuredAdjustment`: Optional dict containing fields like `hours`, `factor`, `minimum_energy_kwh`, `max_grid_kwh`.   
- `DirectiveInterpretation`: `note_index`, `applies`, `directive_type`, `structured_adjustment`, `explanation`.   
- `HourlyPlanItem`: `hour`, `grid_kwh`, `solar_used_kwh`, `battery_action`, `battery_kwh`, `battery_energy_after_kwh`.   
- `ScenarioResponse`: `scenario_id`, `directive_interpretation`, `hourly_plan`, `total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh`, `plan_summary`.   

### 9.3. `app/llm_parser.py`

- Implement `AsyncAnthropic` client.
- Formulate a single prompt that supplies:
  1. The battery parameters (especially `capacity_kwh`) so percentage reserves can be evaluated to numbers.
  2. The exact list of 1-3 operator notes.
- Define a Tool Schema named `submit_interpretations` requiring an array of objects matching `DirectiveInterpretation` in exact note order.
- Use `tool_choice={"type": "tool", "name": "submit_interpretations"}` to guarantee structured JSON output.
- Implement error catching: if API key is missing or Anthropic returns an error/timeout, return a list of fallback `no_op` items.

### 9.4. `app/guardrails.py`

- Accept the raw interpretations and the `BatteryData` object.
- Run programmatic checks:
  - Force length to match number of operator notes.
  - If `directive_type == "no_op"`, set `applies = False` and `structured_adjustment = None`.   
  - If non-`no_op`, set `applies = True`.   
  - Ensure `hours` are unique and sorted ascending within $[0, 23]$.   
  - Clamp `factor` to $[0.0, 1.0]$ for `solar_reduction`.   
  - Clamp `minimum_energy_kwh` to $[0.0, \text{capacity\_kwh}]$.   
  - Ensure `max_grid_kwh` $\ge 0.0$.   
- Return the sanitized `List[DirectiveInterpretation]`.

### 9.5. `app/optimizer.py`

- Build and solve the PuLP model:
  - Initialize `pulp.LpProblem("CampusEnergyOptimization", pulp.LpMinimize)`.
  - Pre-calculate `effective_solar` for all 24 hours.
  - Initialize variables: `grid[h]`, `solar_used[h]`, `charge[h]`, `discharge[h]`, `energy[h]`.
  - Apply operator directive constraints directly to variable bounds and LP constraints.   
  - **Docker Solver Configuration:** Ensure your solver call explicitly supports the installed path, as PuLP defaults can fail inside minimal Debian containers. Resolve the solver path dynamically:
    ```python
    import shutil
    cbc_path = shutil.which("cbc")
    solver = pulp.COIN_CMD(path=cbc_path, msg=False)
    ```
    Then, pass this `solver` instance to your `problem.solve(solver)` function.
  - **Fallback for Infeasible States:** Check status: if optimal, extract variable values. Valid organizer scoring scenarios are guaranteed to be feasible. If an LP fails to solve, it means the LLM extracted conflicting directives or an invalid cap. **Do not relax battery neutrality;** instead, fall back to solving with the directives disabled (`no_op` fallback) so you still earn baseline validity and energy-balance points rather than receiving an automatic zero for battery violation.
  - Assemble `hourly_plan` items and calculate summary metrics (`total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh`, `plan_summary`).   

### 9.6. `app/main.py`

- Initialize FastAPI app with title `"GridWise LLM Energy Optimizer"`.
- Register `GET /health` returning `{"status": "ok"}`.   
- Register `POST /optimize-energy`:
  - Parse and validate incoming `ScenarioRequest`.
  - Call `llm_parser.interpret_notes(req.operator_notes, req.battery)`.
  - Call `guardrails.sanitize_directives(raw_directives, req.battery, len(req.operator_notes))`.
  - Call `optimizer.solve_schedule(req, sanitized_directives)`.
  - Return `ScenarioResponse`.
- Add custom exception handlers for `RequestValidationError` (return 400 or 422).   

### 9.7. `Dockerfile`

```dockerfile
FROM python:3.14-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    coinor-cbc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

## 10. Verification with Sample Case 01

The implementation must be validated against `SAMPLE-01`:

- **Input Notes:**
  1. `"Facilities will wash the rooftop solar panels from noon until 2 PM. During cleaning, usable solar should be treated as roughly 25% of the forecast."`
  2. `"The sports office moved next month's registration deadline."`
- **Expected Directives:**
  - Note 0: `applies: true`, `directive_type: "solar_reduction"`, `hours: [12, 13]`, `factor: 0.25`
  - Note 1: `applies: false`, `directive_type: "no_op"`, `structured_adjustment: null`
- **Expected Reference Metrics:**
  - `total_grid_kwh`: $\approx 2692.5\text{ kWh}$
  - `total_cost_bdt`: $\approx 38365\text{ BDT}$
  - `peak_grid_kwh`: $\approx 175.0\text{ kWh}$
  - End of day energy neutrality: $E_{23} = 110.0\text{ kWh}$ ($= \text{initial\_energy\_kwh}$)

## 11. Instructions for Claude Code Execution

Execute the following steps sequentially to build the project:

1. **Initialize Project Files:**

   Create all directories (`app/`, `tests/`) and write out `requirements.txt`, `.env.example`, and `Dockerfile`.
2. **Generate Python Modules:**

   Implement `schemas.py`, `llm_parser.py`, `guardrails.py`, `optimizer.py`, and `main.py` following the exact mathematical specifications and schemas defined above.
3. **Install Dependencies & Run Tests:**

   Create a virtual environment, install requirements, and create `tests/test_sample_01.py` containing the `SAMPLE-01` JSON payload to verify that:
   - `/health` returns status `200` with `{"status": "ok"}`.   
   - `/optimize-energy` returns the expected directives and schedule within the 0.01 tolerance.   
   - The energy balance equation holds for all 24 hours ($g_h + s_h + d_h == \text{demand}_h + c_h$).   
   - Final battery level equals initial battery level ($E_{23} == E_{\text{initial}}$).   
4. **Prepare Production Artifacts:**
   Ensure no hardcoded API keys exist in the repository. Load `ANTHROPIC_API_KEY` exclusively from environment variables via `python-dotenv`. Prepare `README.md` with copy-pasteable local run commands and `docker run` instructions.
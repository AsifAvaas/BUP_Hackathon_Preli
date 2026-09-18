# GridWise LLM — Smart Campus Energy Optimizer

LLM-assisted operator directive interpretation + PuLP energy scheduling for BUP CSE Fest 2026 Preliminary Round.

## Local Run

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
cp .env.example .env          # fill ANTHROPIC_API_KEY
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## Test

```bash
pytest
```

## Docker

```bash
docker build -t gridwise-llm .
docker run --rm -p 8000:8000 --env-file .env gridwise-llm
```

## Endpoints

- `GET /health` — readiness check.
- `POST /optimize-energy` — scenario + operator notes in, interpretation + 24h schedule out.

See `docs/preli.md` and `docs/gridwise_llm_project_spec.md` for full spec.

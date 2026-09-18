from __future__ import annotations

import logging

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.guardrails import sanitize_directives
from app.llm_parser import interpret_notes
from app.optimizer import solve_schedule
from app.schemas import ScenarioRequest, ScenarioResponse

load_dotenv()

logger = logging.getLogger(__name__)

app = FastAPI(title="GridWise LLM Energy Optimizer")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"detail": "Malformed JSON or structurally invalid request."},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error while processing request")
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/optimize-energy", response_model=ScenarioResponse)
async def optimize_energy(req: ScenarioRequest) -> ScenarioResponse:
    raw_directives = await interpret_notes(req.operator_notes, req.battery)
    directives = sanitize_directives(raw_directives, req.battery, len(req.operator_notes))
    result = solve_schedule(req, directives)
    return ScenarioResponse(**result)

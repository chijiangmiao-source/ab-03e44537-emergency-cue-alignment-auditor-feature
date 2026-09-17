"""FastAPI application exposing the alignment endpoint."""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .align import align
from .schemas import AlignResponse, ErrorResponse, alignment_to_dict
from .validation import (
    INVALID_PAYLOAD,
    ErrorDetail,
    RequestValidationFailed,
    validate_payload,
)

app = FastAPI(
    title="Emergency Insertion Alignment API",
    version="1.0.0",
    description=(
        "Aligns a planned emergency-insertion schedule against the actual "
        "broadcast log with a weighted sequence-difference cost model."
    ),
)


def _error_body(details: list[ErrorDetail]) -> dict:
    return {
        "error": {
            "code": "VALIDATION_FAILED",
            "message": "request body failed validation",
            "details": [
                {"code": d.code, "path": d.path, "message": d.message} for d in details
            ],
        }
    }


@app.exception_handler(RequestValidationFailed)
async def handle_validation_failed(
    request: Request, exc: RequestValidationFailed
) -> JSONResponse:
    return JSONResponse(status_code=422, content=_error_body(exc.details))


@app.exception_handler(RequestValidationError)
async def handle_fastapi_validation(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    details = [
        ErrorDetail(
            code=INVALID_PAYLOAD,
            path=".".join(str(part) for part in error.get("loc", ())),
            message=error.get("msg", "invalid request"),
        )
        for error in exc.errors()
    ]
    if not details:
        details = [ErrorDetail(INVALID_PAYLOAD, "$", "invalid request")]
    return JSONResponse(status_code=422, content=_error_body(details))


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.post(
    "/align",
    response_model=AlignResponse,
    responses={422: {"model": ErrorResponse}},
)
async def post_align(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = None
        malformed = True
    else:
        malformed = False
    if malformed:
        raise RequestValidationFailed(
            [ErrorDetail(INVALID_PAYLOAD, "$", "request body must be valid JSON")]
        )
    planned, actual, alternative_limit = validate_payload(body)
    result = align(planned, actual, alternative_limit=alternative_limit)
    return JSONResponse(content=alignment_to_dict(result))

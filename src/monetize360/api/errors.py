"""Error responses. Not-yours and does-not-exist both return 404 pack_not_found.

Every error leaves in one shape (docs/API.md): {error, message, details, request_id}. Engine errors map to a
status by their code; anything unexpected is a 500 `internal_error` that never leaks a stack trace."""
from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from monetize360.engine import EngineError

log = logging.getLogger("monetize360.api")

ENGINE_STATUS = {
    "pack_invalid": 422,
    "signal_invalid": 422,
    "signal_missing": 422,
    "lookup_miss": 422,
    "bounds_conflict": 422,
    "exclusive_conflict": 409,
    "signal_stale": 503,
    "rule_error": 500,
    "invariant_violation": 500,
}
_HTTP_CODES = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed",
               413: "payload_too_large", 429: "rate_limited"}
_REQUEST_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class ApiError(Exception):
    """An error raised by the API layer itself (auth, unknown pack, bad body)."""

    def __init__(self, status: int, code: str, message: str, details: list[dict] | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or []


def pack_not_found(domain: str) -> ApiError:
    """The same answer whether the domain doesn't exist, isn't live, or belongs to someone else."""
    return ApiError(404, "pack_not_found", f"no live pack for domain '{domain}'")


def unauthorized() -> ApiError:
    return ApiError(401, "unauthorized", "missing or invalid API key")


def jsonable(v: Any) -> Any:
    """Engine values -> JSON: Decimal as a string (never a float), dates as ISO text."""
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set, frozenset)):
        return [jsonable(x) for x in v]
    if v is None or isinstance(v, (str, int, bool)):
        return v
    return str(v)


def error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {"error": code, "message": message, "details": jsonable(details or [])}


def engine_error_body(e: EngineError) -> dict[str, Any]:
    return error_body(e.code, e.message, e.details)


def request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _respond(request: Request, status: int, body: dict[str, Any]) -> JSONResponse:
    rid = request_id(request)
    return JSONResponse({**body, "request_id": rid}, status_code=status, headers={"X-Request-Id": rid})


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def with_request_id(request: Request, call_next):
        # keep the proxy's id when it is a plain token; never echo arbitrary header text back
        incoming = request.headers.get("x-request-id", "")
        request.state.request_id = incoming if _REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    @app.exception_handler(ApiError)
    async def api_error(request: Request, e: ApiError):
        return _respond(request, e.status, error_body(e.code, e.message, e.details))

    @app.exception_handler(EngineError)
    async def engine_error(request: Request, e: EngineError):
        status = ENGINE_STATUS.get(e.code, 500)
        if status >= 500 and e.code != "signal_stale":
            log.error("engine error %s request_id=%s: %s", e.code, request_id(request), e.message)
        return _respond(request, status, engine_error_body(e))

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, e: RequestValidationError):
        details = [{"path": ".".join(str(p) for p in err["loc"] if p != "body"), "message": err["msg"]}
                   for err in e.errors()]
        return _respond(request, 400, error_body("bad_request", "the request body is not valid", details))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, e: StarletteHTTPException):
        code = _HTTP_CODES.get(e.status_code, "bad_request" if e.status_code < 500 else "internal_error")
        return _respond(request, e.status_code, error_body(code, str(e.detail)))

    @app.exception_handler(Exception)
    async def unexpected(request: Request, e: Exception):
        log.exception("unhandled error request_id=%s", request_id(request))
        return _respond(request, 500, error_body("internal_error", "unexpected error; quote the request_id"))

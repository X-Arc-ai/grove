"""Typed errors (B.3): `{"error": {"code", "message", "details", "retryable"}}` with an HTTP status.

An error can never look like an empty result. Messages name a field or a rule, never a value the
caller sent and never a row of the store.
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from grove.store.errors import STORE_UNAVAILABLE

UNAUTHENTICATED = "unauthenticated"
INVALID_ARGUMENTS = "invalid_arguments"
NOT_FOUND = "not_found"
UNAVAILABLE = "unavailable"
INTERNAL = "internal"


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}
        self.retryable = retryable


def error_body(
    code: str, message: str, details: dict[str, Any] | None = None, retryable: bool = False
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "retryable": retryable,
        }
    }


def unauthenticated() -> ApiError:
    return ApiError(401, UNAUTHENTICATED, "sign in to see your Grove")


def invalid_arguments(message: str, field: str) -> ApiError:
    return ApiError(400, INVALID_ARGUMENTS, message, details={"field": field})


def not_found(message: str) -> ApiError:
    return ApiError(404, NOT_FOUND, message)


def _response(error: ApiError) -> JSONResponse:
    return JSONResponse(
        error_body(error.code, error.message, error.details, error.retryable),
        status_code=error.status,
        headers={"Cache-Control": "no-store"},
    )


async def _api_error(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, ApiError)
    return _response(error)


async def _validation_error(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, RequestValidationError)
    errors = error.errors()
    field = ".".join(str(part) for part in errors[0]["loc"]) if errors else "request"
    return _response(invalid_arguments("the request does not match the schema", field))


async def _http_error(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, HTTPException)
    if error.status_code == 404:
        return _response(not_found("no such endpoint"))
    if error.status_code == 401:
        return _response(unauthenticated())
    if error.status_code < 500:
        return _response(ApiError(error.status_code, INVALID_ARGUMENTS, "the request is refused"))
    return _response(ApiError(error.status_code, UNAVAILABLE, "try again later", retryable=True))


async def _unexpected(request: Request, error: Exception) -> JSONResponse:
    if isinstance(error, STORE_UNAVAILABLE):
        return _response(ApiError(503, UNAVAILABLE, "the store cannot be reached", retryable=True))
    return _response(ApiError(500, INTERNAL, "the server failed"))


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(Exception, _unexpected)

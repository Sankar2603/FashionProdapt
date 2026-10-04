import logging

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from fashion_common.middleware import CORRELATION_HEADER, get_correlation_id

logger = logging.getLogger("fashion_common.errors")


class ServiceError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def error_response(status_code: int, code: str, message: str, details=None) -> JSONResponse:
    correlation_id = get_correlation_id()
    body = {"error": {"code": code, "message": message, "correlation_id": correlation_id}}
    if details is not None:
        body["error"]["details"] = jsonable_encoder(details)
    return JSONResponse(status_code=status_code, content=body,
                        headers={CORRELATION_HEADER: correlation_id})


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def handle_service_error(request: Request, exc: ServiceError):
        logger.warning("service error", extra={"code": exc.code, "error": exc.message})
        return error_response(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        return error_response(422, "validation_error", "Request body is invalid.", exc.errors())

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException):
        return error_response(exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception):
        logger.exception("unhandled error")
        return error_response(500, "internal_error", "An unexpected error occurred.")
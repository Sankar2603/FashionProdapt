import logging
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

CORRELATION_HEADER = "X-Correlation-ID"
correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")

logger = logging.getLogger("fashion_common.request")


def get_correlation_id() -> str:
    return correlation_id_var.get()


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        correlation_id = request.headers.get(CORRELATION_HEADER) or uuid.uuid4().hex
        token = correlation_id_var.set(correlation_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
            response.headers[CORRELATION_HEADER] = correlation_id
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            # Health checks run every few seconds; keep them out of INFO logs.
            level = logging.DEBUG if request.url.path == "/health" else logging.INFO
            logger.log(
                level,
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": duration_ms,
                },
            )
            return response
        finally:
            correlation_id_var.reset(token)
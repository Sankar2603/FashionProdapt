
import inspect
from collections.abc import Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from fashion_common.errors import register_error_handlers
from fashion_common.logging_setup import configure_logging
from fashion_common.middleware import CorrelationIdMiddleware

HealthCheck = Callable[[FastAPI], dict[str, bool]]


def create_app(service_name: str, lifespan=None, health_check: HealthCheck | None = None) -> FastAPI:
    configure_logging(service_name)

    app = FastAPI(title=service_name, lifespan=lifespan)
    app.add_middleware(CorrelationIdMiddleware)
    register_error_handlers(app)

    @app.get("/health", tags=["ops"])
    async def health():
        checks = {}
        if health_check is not None:
            result = health_check(app)
            checks = await result if inspect.isawaitable(result) else result
        healthy = all(checks.values())
        return JSONResponse(
            status_code=200 if healthy else 503,
            content={"status": "ok" if healthy else "unhealthy",
                     "service": service_name, "checks": checks},
        )

    return app
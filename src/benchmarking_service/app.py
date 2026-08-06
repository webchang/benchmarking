import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from .auth.jwks import JWKSCache
from .config import settings
from .instances import InstanceRegistry
from .routes import agents, benchmark, benchmarks, config, hello, runs, tools
from .runner.registry import RunRegistry
from .runtime_config import RuntimeConfigStore


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=settings.log_level)
    app.state.registry = InstanceRegistry.load(settings.instances_dir)
    app.state.http = httpx.AsyncClient(timeout=settings.http_timeout_seconds)
    app.state.jwks = JWKSCache(app.state.http, settings.jwks_cache_seconds)
    app.state.runs = RunRegistry()
    app.state.config_overrides = RuntimeConfigStore()
    logging.getLogger(__name__).info(
        "loaded %d instance(s): %s",
        len(app.state.registry),
        ", ".join(sorted(app.state.registry.allowlist)) or "(none)",
    )
    try:
        yield
    finally:
        for task in app.state.runs.in_flight_tasks():
            task.cancel()
        await app.state.http.aclose()


def create_app() -> FastAPI:
    app = FastAPI(title="Benchmarking Service", version="0.1.0", lifespan=lifespan)
    app.include_router(hello.router, tags=["hello"])
    app.include_router(benchmark.router, tags=["benchmark"])
    app.include_router(agents.router)
    app.include_router(tools.router)
    app.include_router(benchmarks.router)
    app.include_router(runs.router)
    app.include_router(config.router, tags=["config"])

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok"}

    _install_openapi(app)
    return app


def _install_openapi(app: FastAPI) -> None:
    """Expose the caller-JWT bearer requirement in the OpenAPI doc.

    Auth is enforced via the `require_caller_jwt` dependency rather than FastAPI's
    security utilities, so it isn't inferred into the schema. Every documented endpoint
    requires the token (only `/healthz` is public, and it's excluded from the schema), so
    we declare a single `bearerAuth` scheme and apply it globally.
    """

    def custom_openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            routes=app.routes,
        )
        schema.setdefault("components", {})["securitySchemes"] = {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": (
                    "Caller JWT. Used only for attribution (preferred_username) and "
                    "issuer-based routing (iss); never forwarded upstream — the Service "
                    "mints its own ROPC token for Rossoctl calls."
                ),
            }
        }
        schema["security"] = [{"bearerAuth": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi


app = create_app()

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

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

    return app


app = create_app()

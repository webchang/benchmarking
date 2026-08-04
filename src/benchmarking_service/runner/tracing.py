"""OTLP span emission to MLflow (the Service's *write* side).

The Service emits the `Agent.Session` trace (+ MCP.CreateSession / Agent.Call /
Evaluator.Evaluate children) straight to MLflow's OTLP/HTTP endpoint (`{tracking_url}/v1/traces`),
skipping the cluster otel-collector. This is what upstream `exgentic_a2a_runner/otel.py` did as the
runner; the Service replaced the runner, so it must emit the trace itself for reports to populate.

Emission is opt-in: `build_tracer` returns None when MLflow isn't configured, and the engine only
emits when handed a tracer — so the untraced path is unchanged.

Export strategy (learned against a live RHOAI MLflow): spans must reach MLflow *as they end during
the run*, not in one burst afterward. The first child span (MCP.CreateSession) has to establish the
trace under our experiment/workspace *before* the agent pod's own spans (propagated via traceparent)
arrive; if our spans are deferred to a single end-of-run flush, MLflow drops them (each POST returns
200, yet the trace never materializes). So we use a `BatchSpanProcessor` tuned to export promptly on
its worker thread (`schedule_delay_millis=200`, `max_export_batch_size=1`) — non-blocking to the
event loop, unlike `SimpleSpanProcessor` whose synchronous per-span export would stall the loop
inside a session's timed window. `flush` (run on `asyncio.to_thread`) force-flushes the root span,
which ends last, before a report is read.
"""

from __future__ import annotations

import logging

from ..models import MLflowConfig

logger = logging.getLogger(__name__)


def build_tracer(cfg: MLflowConfig, token: str):
    """Build an isolated tracer exporting to MLflow's OTLP endpoint.

    Returns `(tracer, flush)` or `None` when `cfg.tracking_url` is unset. `flush` is a blocking
    callable (meant for `asyncio.to_thread`) that exports all buffered spans, then shuts the
    exporter down. The tracer/exporter are isolated (not the global provider) so concurrent
    runs/instances don't share exporter state.
    """
    if not cfg.tracking_url:
        return None

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    headers = {
        "authorization": f"Bearer {token}",
        "x-mlflow-experiment-id": cfg.experiment_id,
    }
    if cfg.workspace:  # RHOAI/multi-tenant MLflow requires a workspace context.
        headers["x-mlflow-workspace"] = cfg.workspace

    session = None
    if cfg.insecure_tls:  # port-forwarded reencrypt endpoints present a self-signed cert.
        import requests

        session = requests.Session()
        session.verify = False

    exporter = OTLPSpanExporter(
        endpoint=f"{cfg.tracking_url.rstrip('/')}/v1/traces",
        headers=headers,
        session=session,
    )

    provider = TracerProvider(resource=Resource.create({"service.name": "benchmarking-service"}))
    provider.add_span_processor(
        BatchSpanProcessor(
            exporter,
            # Export promptly, one span per POST, on the worker thread — so each child span lands
            # *as it ends during the run* (MCP.CreateSession before the agent is even called). This
            # establishes the trace in our experiment before the agent pod's own spans arrive;
            # deferring to a single end-of-run burst causes MLflow to drop our spans.
            schedule_delay_millis=200,
            max_export_batch_size=1,
        )
    )

    def flush() -> None:
        provider.force_flush()  # push the root Agent.Session span (ends last) before a report reads.
        provider.shutdown()

    return provider.get_tracer("benchmarking-service"), flush


def inject_trace_context(headers: dict) -> None:
    """Inject the active span's W3C `traceparent` into an outbound header dict.

    No-op when OpenTelemetry isn't installed or no span is active — so it's safe to call
    unconditionally from the A2A client.
    """
    try:
        from opentelemetry.propagate import inject
    except ImportError:
        return
    inject(headers)

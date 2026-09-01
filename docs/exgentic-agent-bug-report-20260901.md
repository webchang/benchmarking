# exgentic agent — two defects found while benchmarking (2026-09-01)

Reported by the Benchmarking Service team. Both reproduce on a stock agent image with no local
patches. Bug 1 is a correctness problem (token telemetry is silently lost); Bug 2 is pure wasted
latency. They are independent.

## Environment

| | |
|---|---|
| Agent image | `ghcr.io/exgentic/exgentic-a2a-tool_calling:latest` |
| Image digest (index) | `sha256:15a682ceb1deffa72a1b9a78b7ad03ff5614bb86c405cbc33cd9ff35395ec2ee` |
| Agent | `tool_calling`, benchmark `gsm8k` (also seen on `tau2`) |
| Model | `openai/Azure/gpt-5-mini-2025-08-07` via an OpenAI-compatible LiteLLM gateway |
| Agent env | `EXGENTIC_OTEL_ENABLED=true`, `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`, exporting to an otel-collector that forwards to MLflow |
| Runner | reproduced with **both** `EXGENTIC_DEFAULT_RUNNER=direct` and `=service` |

Confirmed identically on two unrelated clusters (single-node KinD on arm64, and OpenShift on amd64),
so it is not environment-specific.

---

## Bug 1 — `TraceLogger._write_otel` throws on the 2nd and later runs against a warm agent, losing the LLM span

### Symptom

The **first** run after an agent pod starts records LLM telemetry correctly. Every **subsequent**
run against the same agent process loses the span for the successful LLM call, so downstream token
accounting reports **0 input / 0 output tokens** even though the task ran and passed.

The task itself is unaffected — tool calls happen, evaluation passes, latencies are recorded. Only
the telemetry is lost, which makes this easy to miss.

### Reproduction (100% reliable)

1. Start a fresh agent pod.
2. Drive one benchmark run (any size). → telemetry correct.
3. Drive a **second** run against the same pod, no restart. → telemetry lost.

Measured, `gsm8k`, `max_parallel_sessions=1` (so this is **not** a concurrency issue):

| | task 0 | task 1 |
|---|---|---|
| run A — first after pod start | `llm=2`, in=320 out=87 ✅ | `llm=2`, in=283 out=23 ✅ |
| run B — same pod, ~30s later | `llm=1`, **in=0 out=0** ❌ | `llm=1`, **in=0 out=0** ❌ |

Repeated a third time on a pod that had served one 50-task run: the next 1-task run again gave
`llm=1, in=0, out=0`.

### Evidence — the exception

Logged by the agent container once per lost span, and **zero times during run A**:

```
TraceLogger._write_otel failed
Traceback (most recent call last):
  File "/app/exgentic/src/exgentic/integrations/litellm/trace_logger.py", line 223, in _write_otel
    self._emit_llm_span(kwargs, response_obj, status, start_time, end_time)
  File "/app/exgentic/src/exgentic/integrations/litellm/trace_logger.py", line 239, in _emit_llm_span
    parent_ctx = self._get_parent_context(kwargs)
                 ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/app/exgentic/src/exgentic/integrations/litellm/trace_logger.py", line 155, in _get_parent_context
    trace_id_hex = ctx.otel_context.trace_id
                   ^^^^^^^^^^^^^^^^
AttributeError: 'NoneType' object has no attribute 'otel_context'
```

So `ctx` is `None` in `_get_parent_context`. The count correlates exactly: a 2-task run B produced
**2** `_write_otel failed` entries and lost **2** spans; run A produced none.

### Evidence — the spans themselves

Read back from the tracing backend for the same benchmark task. Each task normally produces **two**
`chat` spans, which are two *different* LLM calls (see Bug 2), and only the second is the real one:

**Healthy task (run A)** — 2 chat spans:

| span | attrs | notes |
|---|---|---|
| probe | `gen_ai.request.max_tokens=1`, `error.type=BadRequestError` | no usage (expected — see Bug 2) |
| real call | `gen_ai.usage.input_tokens=320`, `gen_ai.usage.output_tokens=87`, `finish_reasons=[tool_calls]`, `gen_ai.response.id=chatcmpl-…` | usage present |

**Affected task (run B)** — 1 chat span:

| span | attrs | notes |
|---|---|---|
| probe | `gen_ai.request.max_tokens=1`, `error.type=BadRequestError`, `has_usage=False` | the **real call's span is absent entirely** |

So this is not "a span written without usage attributes" — the successful call's span is **never
emitted**, consistent with `_emit_llm_span` raising before it writes.

### Impact

- LLM token usage silently under-reports for every run after the first on a given pod. Anything
  built on that telemetry (cost accounting, per-task token analysis) is wrong without any error
  surfacing to the caller.
- It is easy to misdiagnose. We initially attributed the resulting data loss to concurrency and to a
  benchmarking-platform difference; both were wrong. The real variable was whether the run was the
  first one against that pod.

### Suggested fix

`_get_parent_context` assumes `ctx` is non-`None`. Two things worth considering:

1. **Guard it** — if `ctx` (or `ctx.otel_context`) is `None`, fall back to the current OTEL context
   or emit the span without a parent, rather than raising and dropping the span.
2. **Root cause** — the per-process/per-session context appears to be torn down when a run
   completes and is not re-established for the next run, so the second run finds it `None`. A
   guard alone would keep the tokens but the span may end up unparented; re-establishing the
   context per request is probably the more correct fix.

### Our workaround

We now deploy a fresh agent before **every** run. That removes the loss completely (a 10-task run
went from 1 affected task to 0/10, and across two full 12-run matrices from 47 affected rows to 0 of
135 and 0 of 137). It costs ~40–60s per run, so it is viable for us but is clearly a workaround.

---

## Bug 2 — every task issues a `max_tokens=1` probe that always fails, costing a round-trip

### Symptom

Each task performs an extra LLM call with `gen_ai.request.max_tokens=1` which fails with
`BadRequestError` and produces no output. It then proceeds with the real call.

### Evidence

From the healthy trace above, the two spans for one task:

| span | `max_tokens` | outcome | duration |
|---|---|---|---|
| probe | **1** | `error.type=BadRequestError`, no usage, no output messages | **9.58s** |
| real call | — | success, usage 320/87, `finish_reasons=[tool_calls]` | 4.77s |

The probe span starts first and the real call starts only after it ends, so the cost is serial.

### Impact

- **Wasted latency on every task** — in the measured trace the failed probe took *longer than the
  real call* (9.58s vs 4.77s). If that ratio is at all typical, a large fraction of per-task wall
  time is spent on a call that cannot succeed.
- **Inflates call counts.** Anything counting `chat` spans as "LLM calls" overcounts by one per
  task. In our reports a task with a single real call shows `llm=2`.

### Likely cause

`gpt-5-mini` is a reasoning model; the OpenAI-compatible surface rejects `max_tokens` in favour of
`max_completion_tokens`, and/or rejects `max_tokens=1`. We hit exactly this constraint elsewhere:
an independent probe of ours against the same gateway had to use `max_completion_tokens`, and with a
16-token budget the model returned `finish_reason=length` with empty content because the budget was
consumed by reasoning tokens.

### Suggested fix

If the probe is a capability/liveness check, either send `max_completion_tokens` for reasoning
models, skip the probe when the provider/model is known, cache the result per process instead of
per task, or drop it if the information is not used. Worth also confirming the `BadRequestError` is
being handled rather than merely swallowed.

---

## Contact / more data available

We have the full trace JSON for both the healthy and affected tasks (all span names, attributes and
timings), agent logs, and the exact request bodies, and can re-run any variant on demand — including
with a different model if you want to isolate Bug 2 from the reasoning-model behaviour.

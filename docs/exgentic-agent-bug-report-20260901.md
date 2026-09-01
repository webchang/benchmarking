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
accounting under-reports that task's usage even though the task ran and passed.

The task itself is unaffected — tool calls happen, evaluation passes, latencies are recorded. Only
the telemetry is lost, which makes this easy to miss.

**It is even easier to miss than "tokens are 0", because what the damaged row reports depends on the
model.** What survives is Bug 2's probe span, and the probe's own usage differs by model class:

| model | probe outcome | damaged row reports |
|---|---|---|
| `gpt-5-mini` (reasoning) | rejected, `BadRequestError`, no usage | `in=0, out=0` |
| `claude-sonnet-5` | **succeeds** | `in=8, out=1` |
| `gemini-2.5-pro` | **succeeds** | `in=1, out=0` |

So the two bugs interact: **Bug 2 masks Bug 1 on non-reasoning models.** A consumer checking
`tokens == 0` catches only the reasoning-model case. We shipped exactly that check and it reported
our data as clean while 15 rows were in fact damaged — a reviewer spotted "8 tokens in, 1 out" on a
task that had made 11 tool calls. The model-independent signal is structural: **`llm_count <= 1`
together with `tool_count >= 2` is impossible**, because every tool call needs a preceding model
turn.

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

We now deploy a fresh agent before **every** run. Where applied it removes the loss completely: a
10-task run went from 1 affected task to 0/10, and the gsm8k legs of two full 12-run matrices went
from 47 affected rows to 0. It costs ~40–60s per run, so it is viable for us but is clearly a
workaround.

The residual confirms the mechanism rather than contradicting it. Three legs of those matrices still
reused a warm agent, and they are **exactly** the legs with damaged rows: 10 of 20 tau2 tasks and 3
of 15 appworld tasks on OpenShift, 2 of 18 appworld tasks on KinD — 15 of 272 rows overall. Every
leg that deployed fresh is clean, on both clusters.

Two further observations that may help localise the fault:

- **The loss is partial and ordered.** On the 20-task tau2 run, tasks 0–9 lost their spans and tasks
  10–19 recorded correctly, in a run whose first 10 task ids duplicated those of the immediately
  preceding 10-task run on the same pod. It is not "all tasks after the first run fail" — something
  recovers partway through.
- **The corruption is visible without any telemetry backend**, via the arithmetic: that 20-task run
  reported *fewer* total input tokens (692,864) than the 10-task run before it (936,094). Twice the
  tasks, two thirds the tokens.

---

## Bug 2 — the per-task `max_tokens=1` probe is rejected by reasoning models

### Symptom

Each task issues an extra LLM call with `gen_ai.request.max_tokens=1` before the real work. With a
**reasoning** model that call fails with `BadRequestError` and yields nothing; with a non-reasoning
model it succeeds. Either way it is counted as an LLM call.

### Evidence — same benchmark task, two models

`gpt-5-mini` (reasoning) — the probe **fails**:

| span | `max_tokens` | error | usage | dur |
|---|---|---|---|---|
| probe | **1** | **`BadRequestError`** | none | 9.58s |
| real call | — | none | 320 in / 87 out | 4.77s |

`Azure/gpt-4.1` (non-reasoning), same task — the probe **succeeds**:

| span | `max_tokens` | error | usage_in | dur |
|---|---|---|---|---|
| probe | **1** | none | **8** | 13.49s |
| real call | — | none | 239 | 1.62s |
| real call | — | none | 270 | 5.82s |
| real call | — | none | 298 | 1.52s |

The gpt-4.1 totals reconcile exactly: 8 + 239 + 270 + 298 = 815, the value our report shows for that
task. So the probe is a real call that normally returns usage, and the failure is specific to the
reasoning model rejecting `max_tokens`.

### Impact

- **A guaranteed failed round-trip per task on reasoning models.** Every task issues a call that
  cannot succeed. Whether the `BadRequestError` is handled or merely swallowed is worth checking.
- **Call counts are inflated by one.** Anything counting `chat` spans as "LLM calls" overcounts: the
  gpt-4.1 task above made **three** real calls but reports `llm=4`.
- **Latency cost is real but smaller than the span duration suggests.** The probe is always the first
  call in a task and always the slowest (9.58s / 13.49s vs 1.5–5.8s for subsequent calls), which
  points to connection/TLS/gateway warm-up being charged to whichever call goes first. Removing the
  probe would therefore shift most of that cost onto the first real call rather than eliminate it.
  We have not isolated the two, so we are deliberately not claiming a specific saving.

### Likely cause and suggested fix

Reasoning models on the OpenAI-compatible surface reject `max_tokens` in favour of
`max_completion_tokens`. We hit the same constraint independently: a probe of ours against the same
gateway had to use `max_completion_tokens`, and with a 16-token budget `gpt-5-mini` returned
`finish_reason=length` with empty content because reasoning tokens consumed the budget.

Suggested: send `max_completion_tokens` when the model is a reasoning model (or unconditionally, if
the gateway accepts it), and consider caching the probe result per process rather than repeating it
per task.

## Contact / more data available

We have the full trace JSON for both the healthy and affected tasks (all span names, attributes and
timings), agent logs, and the exact request bodies, and can re-run any variant on demand — including
with a different model.

Bug 2's model-dependence is already confirmed here (gpt-5-mini fails, gpt-4.1 succeeds on the same
task). What we have NOT isolated is how much of the probe's wall time is intrinsic versus first-call
connection overhead — that would need a run with the probe disabled, which we cannot do from outside
the agent.

# Benchmark catalog — contributor guide

This package holds the Benchmarking Service's **catalog of runnable benchmarks**. The catalog is
the static `BENCHMARKS` dict in [`registry.py`](./registry.py) — **code, not runtime data**. There
is no database, config file, or admin API behind it, and the HTTP surface over the catalog is
read-only (`GET /benchmarks`, `GET /benchmarks/{name}`).

Adding or changing a benchmark is a **source change + tests + image rebuild + redeploy**, on
purpose: a definition pins container images, dataset env, and resource limits that must move in
lockstep with the workload images. Keeping it in source makes every change a reviewable,
git-tracked, image-versioned artifact instead of mutable per-instance state that could drift.

> Per-instance state (instance config files keyed by `iss`, and the benchmarker-only
> `PUT /config` MLflow/S3 overrides) is the *mutable* part and lives elsewhere — it is **not** in
> this catalog and does not need a rebuild.

## Anatomy of a definition

Each entry is a `BenchmarkDefinition`:

| Field | Purpose |
|---|---|
| `name` | catalog key; also the `{name}` path segment and the `exgentic-mcp-<name>` / `exgentic-a2a-<agent>-<name>` naming root |
| `mcp_image` / `mcp_image_tag` | the MCP tool container image (bump the tag to ship a change) |
| `mcp_port` / `mcp_path` / `mcp_service_suffix` | how the Service dials the tool (`-mcp` Service suffix in-cluster) |
| `tool_env` | env baked into the MCP tool: `BENCHMARK_NAME`, LiteLLM base URL, secret refs, per-benchmark quirks |
| `tool_resources` / agent `resources` | CPU/mem requests+limits sent in the create body (no post-create patch) |
| `default_model` | model used when a deploy/run omits `model` |
| `user_simulator` | **multi-turn flag** — `True` injects `EXGENTIC_SET_BENCHMARK_USER_SIMULATOR_MODEL` into the MCP pod |
| `agents` | map of `BenchmarkAgentSpec` (per-agent `container_image`, `extra_env`, `resources`) |

Declare secret-backed env with the `_secret_env(env_name, secret_name, key)` helper.
`required_secrets()` derives the actionable **`424`** precheck message ("this benchmark requires
secret(s): …") from `tool_env` + the chosen agent's `extra_env`, so a new/changed secret ref wires
that message automatically.

## Adding a benchmark

1. Add a `BenchmarkDefinition` to `BENCHMARKS` (see the existing `gsm8k` / `tau2` / `appworld`
   entries as templates). Reuse the shared `tool_calling` A2A image unless the benchmark needs a
   different agent.
2. Set `user_simulator=True` **only** for multi-turn benchmarks.
3. Add tests in `tests/test_benchmarks.py` asserting `build_tool_request` / `build_agent_request`
   emit the expected images, env, secrets, and resources.
4. Rebuild the Service image, bump its tag, redeploy. The benchmark then shows up in
   `GET /benchmarks` and is deployable/runnable with no client change (request bodies are
   benchmark-agnostic).

## Changing a benchmark

Edit the entry (e.g. bump `mcp_image_tag`, change `default_model`, add env, adjust resources),
update the tests, rebuild, redeploy.

## Per-benchmark env gotchas (do not regress)

These are load-bearing and easy to break — they're commented inline in `registry.py`:

- **gsm8k** — needs `EXGENTIC_SET_BENCHMARK_RUNNER=direct`, and `hf-secret`→`hf-token` for
  HuggingFace dataset access (in addition to `openai-secret`→`apikey`).
- **tau2** — multi-turn: `user_simulator=True` **and** `EXGENTIC_SET_BENCHMARK_ACTION_TIMEOUT=1000`.
  The simulator shares the run's model. Runs are slow — callers must raise `timeout_seconds`.
- **appworld** — **must not** receive `EXGENTIC_SET_BENCHMARK_ACTION_TIMEOUT` (its MCP schema
  rejects `action_timeout` and the pod crashes at startup with "Unknown benchmark override
  'action_timeout'"). No HF token, no simulator, no direct runner.

## Where this is consumed

- `routes/benchmarks.py` — `GET /benchmarks`, `GET /benchmarks/{name}`, `/deploy`, `/status`,
  `/deploy` teardown.
- `routes/runs.py` — `/runs` lifecycle + the deployed/Ready precheck (uses `required_secrets`).
- `build_tool_request` / `build_agent_request` here turn a definition into the rossoctl create
  bodies (MCP_URL / model / OTEL / AuthBridge env injection).

See also `docs/DEVELOPER_GUIDE.md` §8 (task-oriented walkthrough) and
`docs/SERVICE_DESIGN_DECISIONS.md` (why the catalog is static).

#!/usr/bin/env python3
"""Build the 7-section 12-run report from the artifacts run-12.py mirrored.

Usage: gen-12run-report.py <run12-LABEL.json> <version> <platform-note> [out.md]

Reads /tmp/benchmarking/run12-<label>.json plus each run's mirrored report.ndjson /
token_report.ndjson / manifest.json, and emits the fixed 7 sections. Everything numeric is
derived from the artifacts — nothing is transcribed by hand.
"""
import json
import pathlib
import sys

SRC = pathlib.Path(sys.argv[1])
VERSION = sys.argv[2]
PLATFORM = sys.argv[3]
OUT = pathlib.Path(sys.argv[4]) if len(sys.argv) > 4 else None

data = json.loads(SRC.read_text())
runs = sorted(data["runs"], key=lambda r: r["n"])


def rows(run, name):
    d = run.get("mirror_dir")
    if not d:
        return []
    p = pathlib.Path(d) / name
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def manifest(run):
    d = run.get("mirror_dir")
    p = pathlib.Path(d) / "manifest.json" if d else None
    return json.loads(p.read_text()) if p and p.exists() else None


S1 = """## 1. Terms

| Term | Meaning |
|---|---|
| **Benchmark** | A named evaluation suite (`gsm8k`, `tau2`, `appworld`), each backed by an agent + an MCP tool service. |
| **Run** | One invocation of `run_benchmark` against a deployed benchmark (`POST /benchmarks/<b>/runs`). Identified by a `run_id`. A run selects the first `max_tasks` tasks and executes them, up to `max_parallel_sessions` at a time. |
| **Task** | One self-contained benchmark item. Each task runs in **complete isolation**: its own MCP session (`create_session`), its own prompt, one agent call, its own evaluation (`evaluate_session`), and its session is deleted afterward. |
| **Independence** | Tasks within a run are **independent, not a pipeline** — no state flows between them. A task erroring or timing out fails only itself; the batch continues. |
| **Trace / Agent.Session** | Exactly one `Agent.Session` root span per task, keyed by `task_id`. Token usage is parsed from the `chat` (LLM) spans under that root — hence one `token_report` row per task. |
| **Session (`session_id`)** | The per-task MCP session handle from `create_session`. Distinct from `run_id` and `task_id`. |
| **appworld `<hash>_<n>` task ids** | appworld groups several tasks under one base scenario/world (e.g. `3d9a636_1/_2/_3`); each still runs as an independent session. |
"""

S2 = """## 2. Column names & meaning

### `token_report.ndjson` / this report's per-task tables

| Column | Source field | Meaning |
|---|---|---|
| `task` | `task_id` | The benchmark task identifier (one row per task). |
| `in` | `llm_input_tokens` | Sum of `gen_ai.usage.input_tokens` over **all** LLM chat calls in that task. |
| `out` | `llm_output_tokens` | Sum of `gen_ai.usage.output_tokens` over all LLM chat calls in that task. |
| `total` | `llm_total_tokens` | `in + out` for the task. |
| `llm` | `llm_count` | Number of LLM (chat-completion) calls the agent made. One per `chat` span. |
| `passed` | `evaluation_result` | Task-level pass/fail from `evaluate_session` (`None` = errored before eval). |

**Zero-token rows:** `llm>=1` with `in=out=0` means a chat span was recorded without `gen_ai.usage`
attributes. Historically that was the agent-runner/OTEL-context defect at
`max_parallel_sessions > 1`; it is fixed by the `workload_agent_runner` instance override, so a
non-zero count here should be investigated, not assumed benign. `llm=0` means no chat span at all
(session rejected before any model call).

### Run-summary fields (`RunSummary`, shown in §4)

| Field | Meaning |
|---|---|
| `status` | Terminal run status: `succeeded` / `failed` / `error` / `cancelled`. |
| `pass_rate` | `evaluated_pass / total` over the run's tasks. |
| `evaluated_pass` | Count of tasks that passed evaluation. |
| `total` | Number of task results recorded. |
| `wall_seconds` | Wall-clock duration of the run. |
"""


def sec3():
    o = ["## 3. The 12 run requests to the Benchmarking Service", ""]
    o.append("Each run = an optional **deploy** step (teardown + `POST /benchmarks/<b>/deploy`) "
             "followed by a **run** step (`POST /benchmarks/<b>/runs`). Namespace is `team1`, agent "
             "is `tool_calling` throughout.")
    o.append("")
    o.append("*Driver execution order was #1–3, #5–8, #4, #9–10, #11–12 (#4 last among gsm8k "
             "because it swaps the model); listed here in canonical order.*")
    o.append("")
    for r in runs:
        o.append(f"### Run #{r['n']} — {r['title'].split('—',1)[-1].strip()}")
        o.append("")
        o.append(f"- **Benchmark:** `{r['bench']}`")
        models = sorted({x.get("model") for x in rows(r, "report.ndjson") if x.get("model")})
        o.append(f"- **Model in use:** {', '.join(f'`{m}`' for m in models) or '_(no trace)_'}")
        dep = r.get("deploy_request")
        o.append(f"- **Deploy:** {'`'+json.dumps(dep)+'`' if dep else '_(reuses the previous deploy)_'}")
        o.append(f"- **Run request** → `POST /benchmarks/{r['bench']}/runs`:")
        o.append("```json")
        o.append(json.dumps(r["run_request"], indent=2))
        o.append("```")
        o.append("")
    return "\n".join(o)


def sec4():
    o = ["## 4. Summary of the 12 run results", "",
         "| # | Benchmark | run_id | Model in use | Status | Pass | Eval-pass | Err/Total | Wall (s) |",
         "|---|---|---|---|---|---:|---:|---:|---:|"]
    for r in runs:
        s = r.get("summary") or {}
        rr = rows(r, "report.ndjson")
        models = sorted({x.get("model") for x in rr if x.get("model")}) or ["—"]
        err = sum(1 for x in rr if (x.get("status") or "") not in ("OK", ""))
        o.append("| %s | %s | `%s` | %s | %s | %s | %s | %s/%s | %s |" % (
            r["n"], r["bench"], r.get("run_id") or "—", ", ".join(models),
            r.get("status") or "—",
            s.get("pass_rate", "—"), s.get("evaluated_pass", "—"),
            err, s.get("total", "—"),
            round(s["wall_seconds"]) if s.get("wall_seconds") is not None else "—"))
    bad = [(r["n"], sum(1 for x in rows(r, "report.ndjson")
                        if (x.get("llm_count") or 0) > 0 and not x.get("llm_input_tokens")))
           for r in runs]
    bad = [(n, c) for n, c in bad if c]
    o.append("")
    o.append("**Zero-token rows:** " + ("none — token capture is complete across all runs."
             if not bad else ", ".join(f"#{n}: {c}" for n, c in bad) +
             " (investigate: see §2)"))
    return "\n".join(o)


def sec5():
    o = ["## 5. Contents of the manifest file", "",
         "Every run writes a `manifest.json` at its S3 prefix — a self-describing index of the "
         "run's data objects, so a client discovers the whole run in one fetch with no S3 listing. "
         "It lists the 5 data artifacts (`run.json`, `report.ndjson/parquet`, "
         "`token_report.ndjson/parquet`); the manifest does **not** list itself. Each entry carries "
         "`name`, `format`, `key`, public `url`, and `size_bytes`.", ""]
    ex = next((manifest(r) for r in runs if manifest(r)), None)
    if ex:
        o += ["Example:", "", "```json", json.dumps(ex), "```", ""]
    return "\n".join(o)


def sec6():
    o = ["## 6. Per-run aggregate — input & output tokens", "",
         "| # | Benchmark | Model | Tasks | LLM calls | Input | Output | Total | In/task | Out/task |",
         "|---|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in runs:
        rr = rows(r, "report.ndjson")
        if not rr:
            o.append("| %s | %s | — | 0 | — | — | — | — | — | — |" % (r["n"], r["bench"]))
            continue
        i = sum(x.get("llm_input_tokens", 0) or 0 for x in rr)
        ou = sum(x.get("llm_output_tokens", 0) or 0 for x in rr)
        c = sum(x.get("llm_count", 0) or 0 for x in rr)
        models = sorted({x.get("model") for x in rr if x.get("model")}) or ["—"]
        o.append("| %s | %s | %s | %d | %d | %d | %d | %d | %d | %d |" % (
            r["n"], r["bench"], ", ".join(models), len(rr), c, i, ou, i + ou,
            i // len(rr), ou // len(rr)))
    return "\n".join(o)


def sec7():
    o = ["## 7. Per-task detail", ""]
    for r in runs:
        rr = rows(r, "report.ndjson")
        o.append(f"### Run #{r['n']} — {r['bench']}  ·  `{r.get('run_id') or '—'}`")
        o.append("")
        if not rr:
            o.append("_No trace rows (run did not produce a report)._")
            o.append("")
            continue
        o.append("| task | in | out | total | llm | passed |")
        o.append("|---|---:|---:|---:|---:|---|")
        for x in sorted(rr, key=lambda y: str(y.get("task_id"))):
            i = x.get("llm_input_tokens", 0) or 0
            ou = x.get("llm_output_tokens", 0) or 0
            o.append("| %s | %d | %d | %d | %s | %s |" % (
                x.get("task_id"), i, ou, i + ou, x.get("llm_count"), x.get("evaluation_result")))
        o.append("")
    return "\n".join(o)


head = [f"# Benchmarking Service — 12 Parameterized Runs ({PLATFORM})", "",
        f"**Service version:** `{VERSION}`  ", f"**Target:** {data.get('base')}  ",
        f"**Runs executed:** {len(runs)}", "",
        "All numbers below are derived programmatically from the mirrored S3 artifacts "
        "(`report.ndjson` / `token_report.ndjson` / `manifest.json`) — none are transcribed.", ""]

doc = "\n".join(head) + "\n" + "\n\n".join([S1, S2, sec3(), sec4(), sec5(), sec6(), sec7()]) + "\n"
if OUT:
    OUT.write_text(doc)
    print(f"wrote {OUT} ({len(doc)} bytes, {len(runs)} runs)")
else:
    print(doc)

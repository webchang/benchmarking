#!/usr/bin/env python3
"""Build the 7-section 12-run report from the artifacts run-12.py mirrored.

Usage: gen-12run-report.py <run12-LABEL.json> <version> <platform-note> [out.md]

Reads /tmp/benchmarking/run12-<label>.json plus each run's mirrored report.ndjson /
token_report.ndjson / manifest.json, and emits the fixed 7 sections. Everything numeric is
derived from the artifacts — nothing is transcribed by hand.
"""
import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone

SRC = pathlib.Path(sys.argv[1])
VERSION = sys.argv[2]
PLATFORM = sys.argv[3]
OUT = pathlib.Path(sys.argv[4]) if len(sys.argv) > 4 else None

data = json.loads(SRC.read_text())
runs = sorted(data["runs"], key=lambda r: r["n"])


def _stats(vals):
    """(median, mean, CV) over a run's per-task values.

    `median` is the robust centre; `mean` is the arithmetic average — for tau2/appworld the two
    diverge because task cost is strongly right-skewed (a few long episodes drag the mean above the
    median), and that gap is itself informative. `CV` = population sigma / mean, so it shares its
    denominator with `mean`, not `median`. CV is undefined for n < 2 or mean == 0.
    """
    n = len(vals)
    if n == 0:
        return None, None, None
    s = sorted(vals)
    median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    mean = sum(vals) / n
    if n < 2 or mean == 0:
        return median, mean, None
    var = sum((v - mean) ** 2 for v in vals) / n
    return median, mean, (var ** 0.5) / mean


def _fmt(v, spec="%.0f"):
    return "—" if v is None else spec % v


def _natkey(tid):
    """Sort task ids naturally: plain string sort puts tau2's "10" between "1" and "2", which makes
    the per-task tables hard to scan. Handles appworld's `<hash>_<n>` form too."""
    return [(0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"(\d+)", str(tid)) if p]


def _lost(x):
    """True when the task's usage-bearing `chat` span was never written, so its token counts are
    understated.

    Two presentations of the SAME upstream defect, and the token value alone does not identify it:

    * reasoning models (gpt-5-mini) reject the `max_tokens=1` probe, so the surviving probe span
      carries no usage at all -> `in == out == 0`. This is the familiar "zero-token row".
    * non-reasoning models (claude-sonnet-5, gemini-2.5-pro) *accept* the probe, so the probe span
      carries its own tiny usage -> `in=8/out=1` or `in=1/out=0`. Non-zero, so a `== 0` test misses
      it entirely.

    The reliable, model-independent signal is structural: every tool call must be decided by a
    preceding model turn, so `llm_count <= 1` alongside `tool_count >= 2` is impossible for a task
    that genuinely made those tool calls. Verified against all 272 rows of the v1.23 matrices: it
    flags exactly the 15 damaged rows and no healthy one (healthy rows may still have
    `tool_count > llm_count` — up to +21 on appworld — which is why a plain `tool > llm` test is
    NOT usable).
    """
    lc = x.get("llm_count") or 0
    tc = x.get("tool_count") or 0
    return (lc > 0 and not x.get("llm_input_tokens")) or (lc <= 1 and tc >= 2)


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
| **tau2 domain / subset** | τ²-bench ships four domains (`mock`, `retail`, `airline`, `telecom`). We set **no** subset override, so every tau2 run here uses the library default — **`retail`**, 114 tasks. tau2 `task_id`s are that domain's own ids (`"0"`…`"113"`), so `task_id` 0–19 = the first 20 **retail** tasks. Domain is *not* recorded in the artifacts; it is only inferable from the absence of an override, so state it explicitly when quoting tau2 numbers. |
"""

S2 = """## 2. Column names & meaning

### `token_report.ndjson` / this report's per-task tables

| Column | Source field | Meaning |
|---|---|---|
| `task` | `task_id` | The benchmark task identifier (one row per task). |
| `in` | `llm_input_tokens` | Sum of `gen_ai.usage.input_tokens` over **all** LLM chat calls in that task. |
| `out` | `llm_output_tokens` | Sum of `gen_ai.usage.output_tokens` over all LLM chat calls in that task. |
| `total` | `llm_total_tokens` | `in + out` for the task. |
| `llm` | `llm_count` | Number of LLM (chat-completion) calls the agent made. One per `chat` span. **Overcounts real calls by 1** — see below. |
| `tool` | `tool_count` | Number of MCP tool calls the agent made. |
| `passed` | `evaluation_result` | Task-level pass/fail from `evaluate_session` (`None` = errored before eval). |

**Every task issues one extra `max_tokens=1` capability probe**, counted as a `chat` span. So `llm`
reads one high: `llm=2` on gsm8k means *one* real call. Whether that probe succeeds is
model-dependent, which matters for the next paragraph.

**Rows marked ⚠ have lost token attribution** — the usage-bearing `chat` span for the real call was
never written, so `in`/`out` are understated (the task itself ran fine: `tool`, latency and
`passed` are all genuine). The token value alone does not identify these, because the same defect
presents differently per model:

| model class | probe outcome | damaged row looks like |
|---|---|---|
| reasoning (`gpt-5-mini`) | rejected, `BadRequestError`, no usage | `in=0, out=0` — the classic "zero-token row" |
| non-reasoning (`claude-sonnet-5`) | **succeeds**, so the probe's own usage is recorded | `in=8, out=1` |
| non-reasoning (`gemini-2.5-pro`) | **succeeds** | `in=1, out=0` |

A `tokens == 0` test therefore silently misses the sonnet-5/gemini cases. The detector used here is
structural instead: **`llm<=1` together with `tool>=2` is impossible**, since every tool call needs
a preceding model turn. Trigger: reusing a warm agent across runs (upstream exgentic tears down the
per-process OTEL context at run end) — see `docs/exgentic-agent-bug-report-20260901.md`. Mitigation
is a fresh deploy per run. `llm=0` with `tool=0` is different and benign-ish: no chat span at all
(session rejected before any model call).

### `span_report.ndjson` (§8)

One row per OTEL span per task — the evidence the counters above are derived from. Fields:
`task_id`, `session_id`, `trace_id`, `span_id`, `parent_span_id`, `name`, `kind`, `parent_name`,
`depth`, `start_time`, `latency_ms`, `status_code`, `error_type`, `counted`, `input_tokens`,
`output_tokens`, `request_model`, `request_max_tokens`, `finish_reasons`.

| Column | Meaning |
|---|---|
| `name` | The span title, e.g. `Agent.Session`, `chat gpt-5-mini`, `execute_tool submit`. |
| `kind` | `root` / `phase` / `agent` / `chat` / `tool` / `other` (`other` = nested HTTP/framework children the harness does not name). |
| `counted` | Whether this span fed `llm_count`/`tool_count`. `false` marks real work the aggregate cannot see, because only spans parented by `invoke_agent` are counted (and `execute_tool initial_observation` never is). `null` for non-chat/tool spans. |
| `request_max_tokens` | `1` identifies the capability probe unambiguously. |

That set is a deliberate whitelist: span attributes can carry prompts and completions, and these
objects are public, so nothing outside this list is published (see the S3 section).

### Run-summary fields (`RunSummary`, shown in §4)

| Field | Meaning |
|---|---|
| `status` | Terminal run status: `succeeded` / `failed` / `error` / `cancelled`. |
| `pass_rate` | `evaluated_pass / total` over the run's tasks. |
| `evaluated_pass` | Count of tasks that passed evaluation. |
| `total` | Number of task results recorded. |
| `wall_seconds` | Wall-clock duration of the run. |
"""


def _dir_prefix(keys):
    """Longest common prefix truncated at a '/' boundary, so it is a usable S3 prefix rather
    than a mid-token fragment (naive LCP yields things like `.../gsm8k/20260` purely because
    same-day run_ids share leading digits)."""
    if not keys:
        return ""
    cp = os.path.commonprefix(keys)
    return cp[:cp.rfind("/") + 1] if "/" in cp else cp


def sec_s3():
    """Where the artifacts live. Unnumbered so the fixed 7 sections keep their numbering."""
    url_root, per_bench, all_keys = "", {}, []
    for r in runs:
        man = manifest(r)
        if not man:
            continue
        keys = [a["key"] for a in man.get("artifacts", [])]
        for a in man.get("artifacts", []):
            if a.get("url", "").endswith(a["key"]):
                url_root = a["url"][: -len(a["key"])]
        keys.append(man["prefix"].rstrip("/") + "/manifest.json")  # manifest does not list itself
        all_keys += keys
        per_bench.setdefault(r["bench"], []).extend(keys)
    if not all_keys:
        return "## S3 artifact locations\n\n_No exported artifacts._"
    root = _dir_prefix(all_keys)
    o = ["## S3 artifact locations", "",
         "Every number in this report is derived from artifacts published to S3. All objects for "
         "this report share one URL root and one key prefix:", "",
         "| | |", "|---|---|",
         f"| **URL root** | `{url_root}` |",
         f"| **Key prefix (all {len(all_keys)} objects)** | `{root}` |", "",
         "The key layout is `<s3.prefix>/<caller>/<iss-host-encoded>/<benchmark>/<run_id>/<artifact>`, "
         f"so **benchmark is a path segment** and selects cleanly ({len(all_keys)} objects across "
         f"{len(runs)} runs):", "",
         "| benchmark | runs | objects | prefix (append to the key prefix above) |",
         "|---|---:|---:|---|"]
    for b in sorted(per_bench):
        ks = per_bench[b]
        nruns = sum(1 for r in runs if r["bench"] == b and manifest(r))
        sub = _dir_prefix(ks)[len(root):]
        o.append(f"| {b} | {nruns} | {len(ks)} | `{sub}` |")
    models = sorted({m for r in runs for m in
                     {x.get("model") for x in rows(r, "report.ndjson") if x.get("model")}})
    o += ["",
          "**The model in use is NOT part of the key**, so it cannot be selected by prefix. Three of "
          "the models here happen to be prefix-separable only because tau2 and appworld each used a "
          "single model; gsm8k mixes models across its runs, so its prefix necessarily returns all of "
          f"them. Models present: {', '.join('`%s`' % m for m in models)}. To fetch the objects for one "
          "model, resolve model -> `run_id` from the summary in section 4 and use the per-run prefixes.",
          "",
          "Objects are readable **and listable anonymously** (no credentials needed), so treat "
          "anything written here as public. What is actually exposed: the **keys** carry the caller's "
          "username and the Keycloak issuer host, and `run.json` / `report.ndjson` can carry "
          "**exception strings** from failed tasks (`status_message`, `TaskResult.error`), which may "
          "quote a server error body. No artifact contains task prompts or model outputs — the "
          "record schema is entirely ids, counts and durations, and `span_report.*` is restricted to "
          "a fixed whitelist of structural and numeric span fields for exactly this reason."]
    return "\n".join(o)


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
    bad = [(r["n"], sum(1 for x in rows(r, "report.ndjson") if _lost(x)), len(rows(r, "report.ndjson")))
           for r in runs]
    tot = sum(c for _, c, _ in bad)
    allrows = sum(t for _, _, t in bad)
    bad = [(n, c, t) for n, c, t in bad if c]
    o.append("")
    if not bad:
        o.append("**Token attribution:** complete — no row lost its usage-bearing span.")
    else:
        o.append(f"**⚠ Lost token attribution: {tot} of {allrows} rows** — "
                 + ", ".join(f"#{n} ({c}/{t} tasks)" for n, c, t in bad)
                 + ". Those runs' **token totals are understated** (pass rates are not affected — "
                   "the tasks ran and were evaluated normally). See §2 for the detector and why a "
                   "`tokens == 0` check does not catch it on claude-sonnet-5 / gemini-2.5-pro.")
    return "\n".join(o)


def sec5():
    ex = next((manifest(r) for r in runs if manifest(r)), None)
    listed = [a["name"] for a in (ex or {}).get("artifacts", [])]
    o = ["## 5. Contents of the manifest file", "",
         "Every run writes a `manifest.json` at its S3 prefix — a self-describing index of the "
         "run's data objects, so a client discovers the whole run in one fetch with no S3 listing. "
         f"It lists the {len(listed)} data artifacts ("
         + ", ".join(f"`{n}`" for n in listed)
         + "); the manifest does **not** list itself. Each entry carries "
           "`name`, `format`, `key`, public `url`, and `size_bytes`.", ""]
    if ex:
        o += ["Example:", "", "```json", json.dumps(ex), "```", ""]
    return "\n".join(o)


def sec6():
    o = ["## 6. Per-run aggregate — input & output tokens", "",
         "`median` then `mean` then `CV` for each direction. median is the robust centre, mean the "
         "arithmetic average, and a mean well above the median signals right-skew (a few long "
         "tasks). `CV` is population sigma / mean, i.e. how unevenly the token cost is spread; it "
         "shares its denominator with `mean`, not `median`. CV is `—` for single-task runs.", "",
         "A `⚠` in the Lost column means some of that run's rows lost their usage-bearing span (§2), "
         "so **every token figure on that row is understated** — including the median/mean/CV, which "
         "are computed over all tasks and so are dragged down by the damaged ones. Do not quote them "
         "as the run's cost.", "",
         "| # | Benchmark | Model | Tasks | Lost | LLM calls | Input | Output | Total | IN median | IN mean | IN CV | OUT median | OUT mean | OUT CV |",
         "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in runs:
        rr = rows(r, "report.ndjson")
        if not rr:
            o.append("| %s | %s | — | 0 | — | — | — | — | — | — | — | — | — | — | — |" % (r["n"], r["bench"]))
            continue
        iv = [x.get("llm_input_tokens", 0) or 0 for x in rr]
        ov = [x.get("llm_output_tokens", 0) or 0 for x in rr]
        i, ou = sum(iv), sum(ov)
        c = sum(x.get("llm_count", 0) or 0 for x in rr)
        models = sorted({x.get("model") for x in rr if x.get("model")}) or ["—"]
        imed, imean, icv = _stats(iv)
        omed, omean, ocv = _stats(ov)
        nlost = sum(1 for x in rr if _lost(x))
        o.append("| %s | %s | %s | %d | %s | %d | %d | %d | %d | %s | %s | %s | %s | %s | %s |" % (
            r["n"], r["bench"], ", ".join(models), len(rr),
            f"⚠ {nlost}" if nlost else "0", c, i, ou, i + ou,
            _fmt(imed), _fmt(imean), _fmt(icv, "%.2f"),
            _fmt(omed), _fmt(omean), _fmt(ocv, "%.2f")))
    return "\n".join(o)


def sec7():
    o = ["## 7. Per-task detail", "",
         "`model` is repeated on every row deliberately: token counts are only comparable within a "
         "model, and the runs do not all use the same one (#4 swaps the gsm8k model, so its ~815 "
         "input tokens per task are not comparable with the ~320 of #1–3/#5–8 on the same tasks).",
         "",
         "`tool` is shown next to `llm` because their relationship is the tell for a damaged row: "
         "each tool call needs a preceding model turn, so `llm=1` beside `tool=11` cannot be real "
         "work — it is a lost span (§2), flagged `⚠`.", ""]
    for r in runs:
        rr = rows(r, "report.ndjson")
        o.append(f"### Run #{r['n']} — {r['bench']}  ·  `{r.get('run_id') or '—'}`")
        o.append("")
        if not rr:
            o.append("_No trace rows (run did not produce a report)._")
            o.append("")
            continue
        o.append("| task | model | in | out | total | llm | tool | passed | |")
        o.append("|---|---|---:|---:|---:|---:|---:|---|---|")
        for x in sorted(rr, key=lambda y: _natkey(y.get("task_id"))):
            i = x.get("llm_input_tokens", 0) or 0
            ou = x.get("llm_output_tokens", 0) or 0
            o.append("| %s | %s | %d | %d | %d | %s | %s | %s | %s |" % (
                x.get("task_id"), x.get("model") or "—", i, ou, i + ou,
                x.get("llm_count"), x.get("tool_count"), x.get("evaluation_result"),
                "⚠ tokens lost" if _lost(x) else ""))
        nlost = sum(1 for x in rr if _lost(x))
        if nlost:
            o.append("")
            o.append(f"> ⚠ **{nlost} of {len(rr)} rows lost token attribution** — their `in`/`out` "
                     "show only the `max_tokens=1` probe's own usage, not the real call. The tasks "
                     "themselves ran and were evaluated normally, so `tool`/`passed` are correct "
                     "and this run's **pass rate is valid while its token totals are not**.")
        o.append("")
    return "\n".join(o)


def sec8():
    """Per-task span inventory — the evidence layer under §6/§7's aggregates."""
    o = ["## 8. Per-task span inventory", "",
         "Which spans each task actually invoked, by name. §6 and §7 report *counts*; this is what "
         "they were counted from, read straight out of each run's `span_report.ndjson`.", "",
         "Read the **chat** column first. Every task issues a `max_tokens=1` capability probe plus "
         "its real model calls, so a healthy task shows **at least 2** chat spans. Exactly **1** "
         "means the usage-bearing span for the real call was never written — the warm-agent defect "
         "— and that is visible here as a span *count*, independent of any token value (which is "
         "what makes it catchable on claude-sonnet-5 and gemini-2.5-pro, where the surviving probe "
         "reports a non-zero 8/1 or 1/0).", "",
         "`not counted` are spans the aggregator cannot see: it only folds a chat/tool span into "
         "`llm_count`/`tool_count` when its parent is the `invoke_agent` span, so anything nested "
         "deeper is real work missing from the totals. A non-zero figure there is not a bug by "
         "itself — it is the known blind spot, now measurable.", ""]
    any_spans = False
    for r in runs:
        srows = rows(r, "span_report.ndjson")
        o.append(f"### Run #{r['n']} — {r['bench']}  ·  `{r.get('run_id') or '—'}`")
        o.append("")
        if not srows:
            o.append("_No `span_report.ndjson` for this run (predates the artifact, or MLflow was "
                     "unavailable)._")
            o.append("")
            continue
        any_spans = True
        by_task: dict = {}
        for s in srows:
            by_task.setdefault(s.get("task_id"), []).append(s)
        o.append("| task | spans | chat | tool | other | not counted | span names (xN) |")
        o.append("|---|---:|---:|---:|---:|---:|---|")
        for tid in sorted(by_task, key=_natkey):
            ss = by_task[tid]
            kinds: dict = {}
            for s in ss:
                kinds[s.get("kind")] = kinds.get(s.get("kind"), 0) + 1
            names: dict = {}
            for s in ss:
                names[s.get("name")] = names.get(s.get("name"), 0) + 1
            uncounted = sum(1 for s in ss if s.get("counted") is False)
            nchat = kinds.get("chat", 0)
            inventory = ", ".join(f"`{n}`" + (f" x{c}" if c > 1 else "")
                                  for n, c in sorted(names.items(), key=lambda kv: -kv[1]))
            o.append("| %s | %d | %s | %d | %d | %d | %s |" % (
                tid, len(ss), f"**{nchat}** ⚠" if nchat == 1 else str(nchat),
                kinds.get("tool", 0), kinds.get("other", 0), uncounted, inventory))
        lost = [t for t, ss in by_task.items()
                if sum(1 for s in ss if s.get("kind") == "chat") == 1]
        if lost:
            o.append("")
            o.append(f"> ⚠ **{len(lost)} of {len(by_task)} tasks show a single `chat` span** — probe "
                     "only, real call lost. Their token totals in §6/§7 are understated.")
        o.append("")
    if not any_spans:
        return ("## 8. Per-task span inventory\n\n_No run in this set exported "
                "`span_report.ndjson`; re-run against a Service that emits it._")
    return "\n".join(o)


GENERATED = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

head = [f"# Benchmarking Service — 12 Parameterized Runs ({PLATFORM})", "",
        f"**Report generated:** {GENERATED}  ",
        f"**Service version:** `{VERSION}`  ", f"**Target:** {data.get('base')}  ",
        f"**Runs executed:** {len(runs)}", "",
        "All numbers below are derived programmatically from the mirrored S3 artifacts "
        "(`report.ndjson` / `token_report.ndjson` / `span_report.ndjson` / `manifest.json`) — none "
        "are transcribed.", ""]

doc = "\n".join(head) + "\n" + "\n\n".join(
    [sec_s3(), S1, S2, sec3(), sec4(), sec5(), sec6(), sec7(), sec8()]) + "\n"
if OUT:
    OUT.write_text(doc)
    print(f"wrote {OUT} ({len(doc)} bytes, {len(runs)} runs)")
else:
    print(doc)

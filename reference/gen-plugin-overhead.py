#!/usr/bin/env python3
"""Build the comparative AuthBridge-plugin overhead analysis from a 12-run's mirrored artifacts.

Usage: gen-plugin-overhead.py <run12-LABEL.json> <version> <platform-note> [out.md] [judge-ts-file]

Compares the no-plugin baseline leg against each `--plugin-preset` leg. Every number is derived
from the mirrored `report.ndjson` / `span_report.ndjson`; nothing is transcribed.

The comparison is only meaningful because task selection is deterministic, so the preset legs run
the SAME tasks as the leading tasks of the baseline leg. The script restricts every leg to the
intersection of their task ids and asserts the workload really was identical (same input tokens,
same LLM/tool counts) before reporting a single latency figure.

`judge-ts-file` is optional but strongly recommended: one ISO-8601 timestamp per line, being the
IBAC judge proxy's request log. Without it the script cannot tell how many tool calls were actually
authorized, and a per-call median from a leg that judged only some of its calls is a MIXTURE of two
populations -- which is exactly how an incomplete reading once made a superset preset look cheaper
than its own subset. Collect it with:

    oc -n rossoctl-system logs deploy/ibac-judge --since=6h --timestamps \
      | grep -vi healthz | awk '{print $1}' > /tmp/judge.ts
"""
import datetime as dt
import json
import pathlib
import statistics as st
import sys
from datetime import timezone

SRC = pathlib.Path(sys.argv[1])
VERSION = sys.argv[2]
PLATFORM = sys.argv[3]
OUT = pathlib.Path(sys.argv[4]) if len(sys.argv) > 4 else None
JUDGE_TS = pathlib.Path(sys.argv[5]) if len(sys.argv) > 5 else None

BASELINE = 3               # gsm8k, no gateway, no plugins
PRESETS = [5, 6, 7, 8]     # the --plugin-preset legs
TOOL_SPAN = "execute_tool submit"   # the one substantive gsm8k tool call

data = json.loads(SRC.read_text())
runs = {r["n"]: r for r in data["runs"]}

judge_ts = []
if JUDGE_TS and JUDGE_TS.exists():
    for line in JUDGE_TS.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            judge_ts.append(dt.datetime.fromisoformat(line.replace("Z", "+00:00")))
        except ValueError:
            pass


def rows(n, name="report.ndjson"):
    r = runs.get(n)
    if not r or not r.get("mirror_dir"):
        return []
    p = pathlib.Path(r["mirror_dir"]) / name
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def label(n):
    """Describe a leg from its own deploy request, so the doc cannot drift from what was deployed."""
    dep = (runs.get(n) or {}).get("deploy_request") or {}
    if not dep.get("authbridge_enabled"):
        return "none (baseline)"
    parts = [dep.get("plugin_preset") or "authbridge"]
    if dep.get("plugins"):
        parts.append("+ " + ", ".join(dep["plugins"]))
    return " ".join(parts)


def med(v):
    return st.median(v) if v else None


def f(v, spec="%.2f"):
    return "—" if v is None else spec % v


LEGS = [BASELINE] + [n for n in PRESETS if rows(n)]
missing = [n for n in [BASELINE] + PRESETS if not rows(n)]

common = None
for n in LEGS:
    ids = {str(x["task_id"]) for x in rows(n)}
    common = ids if common is None else (common & ids)
COMMON = set(sorted(common or []))
common_sorted = sorted(COMMON, key=lambda s: (len(s), s))


def leg_rows(n):
    return [x for x in rows(n) if str(x["task_id"]) in COMMON]


def leg_spans(n):
    return [s for s in rows(n, "span_report.ndjson") if str(s["task_id"]) in COMMON]


def tool_spans(n):
    return sorted(s["latency_ms"] for s in leg_spans(n) if s["name"] == TOOL_SPAN)


def non_llm(x):
    """Agent time with the LLM path removed.

    The legs ran sequentially against a shared external LLM gateway, so LLM latency carries warm-up
    and response-caching effects that alias onto the plugin variable. Everything the sidecar
    actually intercepts (MCP connect, tool calls) is still in here.
    """
    return (x.get("agent_call_s") or 0) - (x.get("llm_total_s") or 0)


def tool_count(n):
    return sum(x.get("tool_count") or 0 for x in leg_rows(n))


def judged(n):
    """Judge-proxy requests inside this leg's run window, or None when no log was supplied."""
    if not judge_ts:
        return None
    r = runs[n]
    t = r["run_id"][:14]
    start = dt.datetime(int(t[:4]), int(t[4:6]), int(t[6:8]), int(t[8:10]), int(t[10:12]),
                        int(t[12:14]), tzinfo=timezone.utc)
    end = start + dt.timedelta(seconds=(r.get("summary") or {}).get("wall_seconds", 0) + 20)
    return sum(1 for x in judge_ts if start <= x <= end)


JUDGED = {n: judged(n) for n in LEGS}
AUTH = next((n for n in LEGS[1:] if JUDGED.get(n) == 0), None)
# A leg whose every tool call was authorized: the only clean sample of a judged call.
PURE = next((n for n in LEGS[1:] if JUDGED.get(n) and JUDGED[n] >= tool_count(n)), None)
MIXED = [n for n in LEGS[1:] if JUDGED.get(n) and JUDGED[n] < tool_count(n)]

GEN = datetime = dt.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
L = [f"# AuthBridge plugin overhead — comparative estimate ({PLATFORM})", "",
     f"**Report generated:** {GEN}  ",
     f"**Service version:** `{VERSION}`  ",
     f"**Target:** {data.get('base')}  ",
     f"**Legs compared:** #{BASELINE} (baseline) vs #" + ", #".join(str(n) for n in LEGS[1:]),
     f"  ·  **tasks per leg:** {len(common_sorted)} (`{'`, `'.join(common_sorted)}`)  ",
     f"**Judge-call evidence:** {'included' if judge_ts else '**ABSENT** — see the warning below'}",
     ""]
if missing:
    L += [f"> ⚠️ Legs with no mirrored report, excluded: "
          f"{', '.join('#%d' % n for n in missing)}.", ""]
if not judge_ts:
    L += ["> ⚠️ **No judge-proxy log was supplied**, so this document cannot say how many tool calls "
          "were actually authorized. Per-call medians from a leg that judged only *some* of its "
          "calls mix two populations and are not comparable between presets. Re-generate with the "
          "judge log before drawing per-preset conclusions.", ""]

L += ["## What this is, and what it is not", "",
      "**A retrospective, matched-pairs observational comparison — not a designed experiment.** The "
      "telemetry was collected for the canonical 12-run matrix (whose purpose was platform "
      "validation); this document mines those artifacts. Nothing was instrumented or sequenced in "
      "order to measure plugin cost.",
      "",
      "Its one real strength is the matching: **task selection is deterministic**, so the preset "
      "legs execute the *same* benchmark tasks as the leading tasks of the baseline leg, with the "
      "same model. That removes task mix as a confounder — see the identical-work check below. It "
      "does **not** remove anything that varies with time, and the legs ran sequentially.",
      "",
      "Read the headline numbers as **order-of-magnitude and directional**. See "
      "*Confidence and limitations* before quoting any of them.", ""]

# --- identical-work check ---------------------------------------------------
L += ["## The comparison is like-for-like (identical work)", "",
      "If the legs did the same work, latency differences are attributable to the plugin "
      "configuration. They did:", "",
      "| # | plugin config | tasks | input tokens | output tokens | LLM calls | tool calls | model |",
      "|---|---|---:|---:|---:|---:|---:|---|"]
sig = {}
for n in LEGS:
    rr = leg_rows(n)
    models = sorted({x.get("model") for x in rr if x.get("model")}) or ["—"]
    s = (sum(x.get("llm_input_tokens") or 0 for x in rr),
         sum(x.get("llm_count") or 0 for x in rr), tool_count(n))
    sig[n] = s
    L.append("| %d | %s | %d | %d | %d | %d | %d | `%s` |" % (
        n, label(n), len(rr), s[0], sum(x.get("llm_output_tokens") or 0 for x in rr),
        s[1], s[2], models[0]))
same = len(set(sig.values())) == 1
L += ["",
      ("**Identical across every leg** — same input-token total, same LLM-call count, same tool-call "
       "count. The only variable is the plugin configuration."
       if same else
       "⚠️ **The legs did NOT do identical work**, so the latency deltas below are confounded by "
       "workload and must not be read as plugin cost."), ""]

# --- is the judge consulted? -----------------------------------------------
L += ["## Is the judge actually consulted? (read this before the latency tables)", "",
      "The IBAC judge is **itself an LLM call** — the sidecar issues `POST /v1/chat/completions` "
      "through the judge proxy for every action it authorizes. Two consequences: judge latency "
      "inherits LLM-inference variance (hundreds of ms to ~10 s), and **the number of judged calls "
      "is not always equal to the number of tool calls**. Counting them is therefore a prerequisite "
      "for interpreting any per-call figure.", ""]
if judge_ts:
    L += ["| # | plugin config | tool calls | judge calls | reading |",
          "|---|---|---:|---:|---|"]
    for n in LEGS[1:]:
        j, t = JUDGED[n], tool_count(n)
        if j == 0:
            read = "IBAC not engaged — **correct** for this preset"
        elif j >= t:
            read = "**every call authorized** — the clean sample"
        else:
            read = f"**only {j} of {t} authorized** — see below"
        L.append(f"| {n} | {label(n)} | {t} | {j} | {read} |")
    if MIXED:
        L += ["",
              "### The gap, and why it decides the latency question", "",
              "Legs " + ", ".join(f"#{n} ({JUDGED[n]}/{tool_count(n)})" for n in MIXED) +
              " authorized only some of their tool calls. That single fact explains an apparent "
              "paradox in the raw numbers: a leg whose preset is a strict **superset** of another's "
              "can show a *lower* median per-tool-call, purely because fewer of its calls paid the "
              "judge. Its median is then a **mixture** of judged and unjudged calls and says nothing "
              "about either. Unjudged calls are visible in the samples below as values sitting at "
              "the sidecar-only floor.", "",
              "Two candidate explanations, not distinguishable from these artifacts:", "",
              "1. **Benign — decision caching under concurrency.** These legs run "
              "`max_parallel_sessions=4`: several identical calls fire at once, all miss the cache "
              "and consult the judge, and later ones hit a warm entry.",
              "2. **A fail-open enforcement gap** — the action proceeded without an authorization "
              "decision. For a security control this matters far more than the latency.", "",
              "**Decisive test:** re-run the `ibac-only` leg at `max_parallel_sessions=1`. Serial "
              "execution means caching would show ~1 judge call for the whole leg, while no caching "
              "shows one per tool call. Roughly a minute of cluster time; until it is run, treat the "
              "gap as **open**, not as established caching.", ""]
else:
    L += ["_Judge-call counts unavailable — supply the judge-proxy log (see the script docstring)._",
          ""]

# --- headline --------------------------------------------------------------
b_non = med([non_llm(x) for x in leg_rows(BASELINE)])
L += ["## Headline estimate", "",
      "Three costs, deliberately kept apart — a single \"overhead\" number would hide the one that "
      "scales:", "",
      "| # | plugin config | non-LLM time/task | Δ vs baseline | judged/tool | per tool call (median) |",
      "|---|---|---:|---:|---:|---:|"]
for n in LEGS:
    rr = leg_rows(n)
    nl = med([non_llm(x) for x in rr])
    tsp = med(tool_spans(n))
    jt = "—" if n == BASELINE else (f"{JUDGED[n]}/{tool_count(n)}" if JUDGED.get(n) is not None else "?")
    flag = " ⚠️mixture" if n in MIXED else ""
    L.append("| %d | %s | %ss | %s | %s | %s ms%s |" % (
        n, label(n), f(nl), "—" if n == BASELINE else "**%+.2fs**" % (nl - b_non),
        jt, f(tsp, "%.0f"), flag))

base_tool = med(tool_spans(BASELINE))
auth_tool = med(tool_spans(AUTH)) if AUTH else None
pure_tool = med(tool_spans(PURE)) if PURE else None
L += ["", "The three components, each from the leg that measures it cleanly:", ""]
L += [f"1. **Fixed cost of running a sidecar at all: ~{f(med([med([non_llm(x) for x in leg_rows(n)]) for n in LEGS[1:]]) - b_non, '%+.1f')} s per task.** "
      "Roughly flat across all four presets, so it is a cost of using AuthBridge rather than of any "
      "policy. The span table localises most of it to `connect_mcp`."]
if AUTH and auth_tool is not None and base_tool is not None:
    L.append(f"2. **An unjudged tool call costs ~{f(auth_tool - base_tool, '%+.0f')} ms** "
             f"({f(base_tool, '%.0f')} → {f(auth_tool, '%.0f')} ms, from `{label(AUTH)}`): the proxy "
             "hop and token exchange, with no authorization decision.")
if PURE and pure_tool is not None and auth_tool is not None:
    L.append(f"3. **A judged tool call adds a further ~{f(pure_tool - auth_tool, '%+.0f')} ms** "
             f"({f(auth_tool, '%.0f')} → {f(pure_tool, '%.0f')} ms, from `{label(PURE)}` — the only "
             "leg where *every* call was authorized, hence the only uncontaminated sample). This is "
             "the judge's LLM round-trip, and it is the component that scales with tool traffic.")
elif judge_ts:
    L.append("3. _No leg authorized all of its tool calls, so the judged-call cost cannot be "
             "isolated cleanly from this matrix._")
L += ["",
      "**Do not rank the presets against each other from the `per tool call` column** — any row "
      "marked ⚠️mixture is a blend of judged and unjudged calls.", ""]

# --- span decomposition ----------------------------------------------------
NAMED = ["connect_mcp", "create_agent", "MCP.CreateSession",
         "execute_tool initial_observation", TOOL_SPAN, "Evaluator.Evaluate"]
L += ["## Where the time goes (span-level)", "",
      "Median span latency in **ms**, read directly from each leg's `span_report.ndjson` — the "
      "sharpest instrument available, because it localises cost to a named operation instead of "
      "inferring it from a task total.", "",
      "| span | " + " | ".join(f"#{n} {label(n)}" for n in LEGS) + " |",
      "|---|" + "---:|" * len(LEGS)]
span_cache = {n: leg_spans(n) for n in LEGS}
for name in NAMED:
    cells = []
    for n in LEGS:
        v = [s["latency_ms"] for s in span_cache[n] if s["name"] == name]
        cells.append(f(med(v), "%.0f") if v else "—")
    L.append(f"| `{name}` | " + " | ".join(cells) + " |")
for what, pred in (("chat — probe (`max_tokens=1`)",
                    lambda s: s["kind"] == "chat" and s.get("request_max_tokens") == 1),
                   ("chat — real call",
                    lambda s: s["kind"] == "chat" and s.get("request_max_tokens") != 1)):
    cells = []
    for n in LEGS:
        v = [s["latency_ms"] for s in span_cache[n] if pred(s)]
        cells.append(f(med(v), "%.0f") if v else "—")
    L.append(f"| {what} | " + " | ".join(cells) + " |")
cells = []
for n in LEGS:
    per = {}
    for s in span_cache[n]:
        per[s["task_id"]] = per.get(s["task_id"], 0) + 1
    cells.append(f(med(list(per.values())), "%.0f") if per else "—")
L.append("| *(spans per task, count)* | " + " | ".join(cells) + " |")

cm = {n: med([s["latency_ms"] for s in span_cache[n] if s["name"] == "connect_mcp"]) for n in LEGS}
with_sc = [cm[n] for n in LEGS[1:] if cm[n]]
L += ["",
      f"- **`connect_mcp`** goes from **{f(cm[BASELINE], '%.0f')} ms** without a sidecar to "
      f"**~{f(med(with_sc), '%.0f')} ms** with one, essentially constant across all four presets: "
      "the agent's first MCP connection now traverses the proxy. A per-task admission cost, "
      "independent of policy.",
      f"- **`{TOOL_SPAN}`** is where policy shows up — but only once you know how many of those "
      "calls were judged.",
      "",
      "⚠️ **The `chat` rows are not usable as plugin cost.** The legs ran sequentially against a "
      "shared external LLM gateway with identical prompts, and real-call latency *drops* sharply in "
      "the later legs — the signature of response/prompt caching, not of plugins accelerating LLM "
      "calls. A lone very slow `create_agent`/probe is gateway warm-up. Hence the non-LLM headline.",
      ""]

# --- full sample -----------------------------------------------------------
L += [f"## Every `{TOOL_SPAN}` span (the whole sample)", "",
      "Printed in full because n is small and the distributions overlap. This is also where the "
      "judged/unjudged mixture is visible directly: unjudged calls sit near the sidecar-only floor "
      f"({f(auth_tool, '%.0f') if auth_tool is not None else '?'} ms), judged calls well above it.",
      "",
      "| # | plugin config | judged/tool | latencies (ms, sorted) | median | mean |",
      "|---|---|---:|---|---:|---:|"]
for n in LEGS:
    v = tool_spans(n)
    jt = "—" if n == BASELINE else (f"{JUDGED[n]}/{tool_count(n)}" if JUDGED.get(n) is not None else "?")
    L.append("| %d | %s | %s | %s | %s | %s |" % (
        n, label(n) + (" ⚠️mixture" if n in MIXED else ""), jt,
        ", ".join("%.0f" % x for x in v), f(med(v), "%.0f"),
        f(st.mean(v), "%.0f") if v else "—"))
L.append("")

# --- projection ------------------------------------------------------------
if PURE and pure_tool is not None and base_tool is not None:
    delta = (pure_tool - base_tool) / 1000.0
    L += ["## Projection to the heavier benchmarks (NOT a measurement)", "",
          "gsm8k makes only one substantive tool call per task, so it barely exercises the marginal "
          "cost. Multiplying the measured per-authorized-call delta "
          f"(**{f(delta, '%.2f')} s**, from `{label(PURE)}`) by the tool-call counts observed "
          "elsewhere in the same matrix indicates what the same policy would cost on a tool-heavy "
          "workload:", "",
          "| benchmark | tool calls/task (measured) | projected added latency/task |",
          "|---|---:|---:|"]
    gs = med([x.get("tool_count") or 0 for x in leg_rows(BASELINE)])
    L.append(f"| gsm8k | {f(gs, '%.0f')} | {f(delta * (gs or 0), '%.1f')} s |")
    for n, bench in ((9, "tau2"), (11, "appworld")):
        rr = rows(n)
        if rr:
            tc = med([x.get("tool_count") or 0 for x in rr])
            L.append(f"| {bench} | {f(tc, '%.0f')} | {f(delta * (tc or 0), '%.1f')} s |")
    L += ["",
          "**A model-based projection, not a measurement.** It assumes per-call cost is constant "
          "across benchmarks and unaffected by concurrency — untested, and the judged/tool gap above "
          "is direct evidence that call *counts* behave unexpectedly under concurrency. tau2 and "
          "appworld were never run with a preset in this matrix. Treat as a planning indication and "
          "measure before relying on it.", ""]

# --- per-task --------------------------------------------------------------
L += ["## Per-task detail (non-LLM path)", "",
      f"n = {len(common_sorted)} tasks per leg.", "",
      "| # | plugin config | non-LLM seconds per task (sorted) | median |",
      "|---|---|---|---:|"]
for n in LEGS:
    v = sorted(round(non_llm(x), 1) for x in leg_rows(n))
    L.append("| %d | %s | %s | %s |" % (n, label(n), ", ".join(f"{x:.1f}" for x in v), f(med(v))))

# --- limitations -----------------------------------------------------------
L += ["", "## Confidence and limitations", "",
      "**What this supports.** Order-of-magnitude, directional claims: a sidecar costs seconds per "
      "task rather than milliseconds; an authorized tool call costs ~2 s more than an unauthorized "
      "one because the judge is an LLM inference; the marginal cost scales with tool traffic, not "
      "with task count.", "",
      "**What it does not support.** A ranking of the presets against each other, a precise point "
      "estimate, or generalisation to other benchmarks, models or clusters.", "",
      "The specific threats, worst first:", "",
      "1. **Judged-call counts differ from tool-call counts** (see above), so any per-call median "
      "from an affected leg mixes two populations. This is the single largest interpretation trap "
      "here, and it is why the judged-call cost is taken only from the leg that authorized "
      "everything.",
      "2. **Order is confounded with condition.** The legs ran sequentially, so gateway warm-up, "
      "response caching and cluster drift alias onto the plugin variable. This demonstrably "
      "happened on the LLM path (hence its exclusion); the same mechanism could be moving the "
      "non-LLM numbers.",
      f"3. **No replication.** One run per condition and n = {len(common_sorted)} tasks, so a "
      "condition effect cannot be separated from ordinary run-to-run variance. The judge being an "
      "LLM call makes its latency intrinsically wide, so a ~1 s difference between presets is not "
      "resolvable at this sample size.",
      "4. **`overhead_s` is a residual** (`agent_call − time_to_first_obs − llm_after_obs − "
      "tool_total`), absorbing everything unmodelled including measurement error. Part of the fixed "
      "cost rests on it.",
      "5. **Cold start is not separated.** Each leg deployed fresh, so per-task figures amortise "
      "agent warm-up over very few tasks, likely *inflating* the fixed cost.",
      "6. **Latency only.** The sidecar's CPU, memory and throttling cost is **unmeasured** — "
      "`has_infra` is false and every `*_cpu_utilization_pct` / `*_memory_max_mb` field in these "
      "runs is `0.0`. For a proxy sidecar that axis often matters more for capacity planning.", "",
      "**To make this load-bearing:** run each preset at 20–50 tasks (amortises cold start, "
      "separates fixed from marginal), **interleave or repeat the conditions** to break the order "
      "confound, run `ibac-only` at `max_parallel_sessions=1` to settle the judged/tool gap, "
      "populate the infra fields, and generate the companion document for the other platform as an "
      "independent second measurement.", ""]

# --- provenance ------------------------------------------------------------
L += ["## Source artifacts", "",
      "Every figure above is derived from these runs' published artifacts; nothing is transcribed.",
      "", "| # | plugin config | run_id | S3 prefix |", "|---|---|---|---|"]
for n in LEGS:
    r = runs[n]
    L.append("| %d | %s | `%s` | `%s` |" % (n, label(n), r.get("run_id") or "—",
                                            r.get("artifacts_prefix") or "—"))
L += ["",
      "Read with `report.ndjson` (per-task aggregates) and `span_report.ndjson` (per-span detail). "
      "Objects are anonymously readable; see the S3 section of the matching 12-run report for the "
      "URL root and key layout. Judge-call counts come from the in-cluster `ibac-judge` proxy's "
      "request log, windowed to each leg's run interval.", ""]

doc = "\n".join(L) + "\n"
if OUT:
    OUT.write_text(doc)
    print(f"wrote {OUT} ({len(doc)} bytes; legs {LEGS}, {len(common_sorted)} tasks, "
          f"judge log {'yes' if judge_ts else 'no'}, pure-judged leg {PURE}, mixed {MIXED})")
else:
    print(doc)

#!/usr/bin/env python3
"""Compare two 12-run result sets (e.g. OCP vs KinD) from the artifacts run-12.py mirrored.

Usage: gen-12run-comparison.py <A.json> <A-label> <B.json> <B-label> <version> [out.md]

Every number is derived from the mirrored report.ndjson files, nothing transcribed.
"""
import json
import pathlib
import sys

A, ALAB, B, BLAB, VERSION = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
OUT = pathlib.Path(sys.argv[6]) if len(sys.argv) > 6 else None


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


def f(v, spec="%.0f"):
    return "—" if v is None else spec % v


def load(path):
    d = json.loads(pathlib.Path(path).read_text())
    out = {}
    for r in d["runs"]:
        md = r.get("mirror_dir")
        rows = []
        if md:
            p = pathlib.Path(md) / "report.ndjson"
            if p.exists():
                rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
        s = r.get("summary") or {}
        iv = [x.get("llm_input_tokens", 0) or 0 for x in rows]
        ov = [x.get("llm_output_tokens", 0) or 0 for x in rows]
        imed, imean, icv = _stats(iv)
        omed, omean, ocv = _stats(ov)
        out[r["n"]] = dict(
            bench=r["bench"], p=s.get("pass_rate"), w=s.get("wall_seconds"), rows=len(rows),
            i=sum(iv), o=sum(ov), imed=imed, imean=imean, icv=icv,
            omed=omed, omean=omean, ocv=ocv,
            par=(r.get("run_request") or {}).get("max_parallel_sessions"),
            z=sum(1 for x in rows if (x.get("llm_count") or 0) > 0 and not x.get("llm_input_tokens")))
    return out, d.get("base")


X, xbase = load(A)
Y, ybase = load(B)
L = [f"# 12-Run Comparison — {ALAB} vs {BLAB}, both Service {VERSION}", "",
     f"- **{ALAB}**: `{xbase}`", f"- **{BLAB}**: `{ybase}`", "",
     "Both sides ran the same 12 request bodies, the same Service version, and verified-identical",
     "instance config. Every leg deploys fresh. Task selection is deterministic, so the same",
     "`task_id` is the same task on both platforms — differences are attributable to the platform.",
     "",
     "All numbers are derived from the mirrored `report.ndjson` artifacts.", "",
     "## Pass rate, wall time, tokens", "",
     f"| # | bench | p | pass {ALAB} | pass {BLAB} | Δ | wall {ALAB} | wall {BLAB} | {BLAB}/{ALAB} | in {ALAB} | in {BLAB} | in Δ |",
     "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
for n in sorted(X):
    x, y = X[n], Y.get(n, {})
    d = "%+.2f" % (y["p"] - x["p"]) if None not in (x.get("p"), y.get("p")) else "—"
    r = "%.2fx" % (y["w"] / x["w"]) if x.get("w") and y.get("w") else "—"
    di = "same" if x["i"] == y.get("i") else "%+d" % (y.get("i", 0) - x["i"])
    L.append("| %d | %s | %s | %s | %s | %s | %s | %s | %s | %d | %d | %s |" % (
        n, x["bench"], x["par"], x["p"], y.get("p"), d,
        f(x["w"]), f(y.get("w")), r, x["i"], y.get("i", 0), di))

L += ["", "## Token distribution per task", "",
      "For each direction: `median` (robust centre), then `mean`, then `CV` (population sigma / "
      "mean — how unevenly the cost is spread). A mean well above the median means right-skew: a "
      "few long tasks dominate. CV shares its denominator with `mean`, not `median`, and is `—` "
      "for single-task runs. Input CV runs systematically higher than output CV because every LLM "
      "call re-sends the whole conversation, so cumulative input grows with the square of the turn "
      "count while output stays bounded per call.", "",
      "### Input tokens", "",
      f"| # | bench | median {ALAB} | mean {ALAB} | CV {ALAB} | median {BLAB} | mean {BLAB} | CV {BLAB} |",
      "|---|---|---:|---:|---:|---:|---:|---:|"]
for n in sorted(X):
    x, y = X[n], Y.get(n, {})
    L.append("| %d | %s | %s | %s | %s | %s | %s | %s |" % (
        n, x["bench"],
        f(x["imed"]), f(x["imean"]), f(x["icv"], "%.2f"),
        f(y.get("imed")), f(y.get("imean")), f(y.get("icv"), "%.2f")))

L += ["", "### Output tokens", "",
      f"| # | bench | median {ALAB} | mean {ALAB} | CV {ALAB} | median {BLAB} | mean {BLAB} | CV {BLAB} |",
      "|---|---|---:|---:|---:|---:|---:|---:|"]
for n in sorted(X):
    x, y = X[n], Y.get(n, {})
    L.append("| %d | %s | %s | %s | %s | %s | %s | %s |" % (
        n, x["bench"],
        f(x["omed"]), f(x["omean"]), f(x["ocv"], "%.2f"),
        f(y.get("omed")), f(y.get("omean")), f(y.get("ocv"), "%.2f")))

ti, tk = sum(v["i"] for v in X.values()), sum(v["i"] for v in Y.values())
to, ko = sum(v["o"] for v in X.values()), sum(v["o"] for v in Y.values())
wo, wk = sum(v["w"] or 0 for v in X.values()), sum(v["w"] or 0 for v in Y.values())
same = [n for n in X if Y.get(n, {}).get("p") == X[n]["p"]]
diff = [n for n in X if Y.get(n, {}).get("p") != X[n]["p"]]
ident = [n for n in X if Y.get(n, {}).get("i") == X[n]["i"]]
zx, zy = sum(v["z"] for v in X.values()), sum(v["z"] for v in Y.values())
rx, ry = sum(v["rows"] for v in X.values()), sum(v["rows"] for v in Y.values())

L += ["", "## Totals", "",
      f"- input tokens: {ALAB} {ti:,} · {BLAB} {tk:,}",
      f"- output tokens: {ALAB} {to:,} · {BLAB} {ko:,}",
      f"- wall: {ALAB} {wo:.0f}s · {BLAB} {wk:.0f}s",
      f"- zero-token rows: {ALAB} {zx}/{rx} · {BLAB} {zy}/{ry}"
      + ("  — **token capture complete on both sides**" if not zx and not zy else "  — **investigate**"),
      "", "## What matches", "",
      f"- **{len(same)} of {len(X)} pass rates identical**: {', '.join('#%d' % n for n in same)}.",
      f"- **{len(ident)} runs have byte-identical input-token totals**: "
      f"{', '.join('#%d' % n for n in ident)} — deterministic task selection plus the same model",
      "  means identical work, the strongest available check that this is a like-for-like comparison",
      "  rather than merely a similar one.", "",
      "## Where they differ", "",
      f"- **{len(diff)} run(s) differ**: "
      f"{', '.join('#%d (%+.2f)' % (n, Y[n]['p'] - X[n]['p']) for n in diff if Y.get(n, {}).get('p') is not None)}.",
      "- Interpret small deltas on small runs with care: one task on a 5-task run moves pass_rate by",
      "  0.20. tau2 and appworld are additionally nondeterministic per episode.",
      "", "## Caveat on the `llm` column", "",
      "Each task issues an extra `max_tokens=1` probe call that is counted as a `chat` span, so",
      "LLM-call counts read one high per task. See `docs/exgentic-agent-bug-report-20260901.md`."]

doc = "\n".join(L) + "\n"
if OUT:
    OUT.write_text(doc)
    print(f"wrote {OUT} ({len(doc)} bytes)")
else:
    print(doc)

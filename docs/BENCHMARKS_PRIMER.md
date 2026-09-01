# The three benchmarks, for people new to them

We run three benchmarks through the Benchmarking Service: **gsm8k**, **tau2**, and **appworld**.
They are not three interchangeable test suites — they form a deliberate difficulty ladder, and each
one stresses a different part of the agent stack. This note explains what each is, what a single
task actually involves, and what they look like in practice.

All the "measured" figures below come from our own v1.23 runs (272 tasks across two clusters:
OpenShift `ykt3→ykt2` and single-node KinD), not from the benchmarks' published papers.

Per-task token averages are computed over the **237 rows with intact telemetry**, excluding 15 rows
whose usage-bearing span was lost (see the last bullet of "How to read our reports"). Including them
understates tau2 by 17% and appworld by 12%; pass rates and latencies use all 272 rows, since those
are unaffected.

---

## At a glance

| | **gsm8k** | **tau2** | **appworld** |
|---|---|---|---|
| What it tests | multi-step arithmetic reasoning | multi-turn dialogue + tool use | long-horizon app automation |
| Tasks measured | 172 | 60 (50 clean) | 40 (35 clean) |
| **Pass rate** | **0.98** | **0.83** | **0.00** |
| Input tokens / task | **343** | **82,966** | **210,792** |
| Output tokens / task | 205 | 1,955 | 21,499 |
| LLM calls / task | 2.1 | 11.4 | 23.6 |
| Tool calls / task | 1.1 | 11.2 | 13.4 |
| Median task latency | **5.3s** | **92s** | **232s** |
| Slowest task seen | 62s | 425s | 581s |
| Model we use | gpt-5-mini (or gpt-4.1) | claude-sonnet-5 | gemini-2.5-pro |
| Task pool | 8.5K problems (HuggingFace) | 114 (`retail` domain) | grouped scenarios |
| `task_id` format | integer (`0`, `1`, …) | integer (`0`, `1`, …) | `21abae1_1` |

The headline is the **scale gap**: a tau2 task costs ~240× the input tokens of a gsm8k task, and an
appworld task ~615×. Choose accordingly — a 50-task gsm8k run is a couple of minutes; a 20-task
appworld run is half an hour and millions of tokens.

---

## gsm8k — the smoke test

**What it is.** GSM8K — **"Grade School Math 8K"**, where the *8K* is the dataset size: ~8.5K
problems (7,473 train + 1,319 test). They are grade-school maths word problems needing **2–8
elementary steps** (+ − × ÷); no algebra or geometry. Each has one correct numeric answer, graded by
**exact match**. The difficulty is not the arithmetic but carrying a multi-step chain without
slipping — which is why it became a standard reasoning probe.

**Where the data comes from.** The MCP (`ghcr.io/exgentic/exgentic-mcp-gsm8k`) loads the dataset from
**HuggingFace** at startup. That is why an `hf-secret` must *exist* in the namespace even with an
empty value (the dataset is public) — without it the MCP pod sits in `CreateContainerConfigError` and
the agent crash-loops.

**What one task looks like.** The agent receives a word problem, thinks, then calls a tool to submit
its answer. In our harness a typical task is **one real LLM call and one tool call** — it is close to
the simplest possible agentic loop.

**What it stresses.** Almost nothing about the *platform* — which is exactly why it is useful. If
gsm8k fails, the problem is infrastructure (deploy, auth, LLM reachability, telemetry), not agent
capability. It is our canary.

**What to expect.** ~0.98 pass rate, ~5s per task, ~343 input tokens. Deterministic enough that the
same task produces the *same token count* on different clusters — we use that as a correctness check
that two environments are genuinely comparable.

---

## tau2 — the conversational one

**What it is.** τ²-bench evaluates an agent acting as a support agent in a tool-backed domain: it
must hold a **multi-turn conversation** while calling domain APIs to actually accomplish the request.

**Which domain.** τ²-bench ships four — `mock`, `retail`, `airline`, `telecom`. We set **no** subset
override, so we get the library default: **`retail`** (114 tasks). tau2 `task_id`s are that domain's
own ids, so `task_id` 0–19 are the first 20 *retail* tasks. Worth stating explicitly whenever you
quote a tau2 number, because **the domain is not recorded in the artifacts** — it is only inferable
from the absence of an override. Switching domains would change the numbers, and `airline` has only
50 tasks, so `max_tasks` above that would silently cap.

**The key architectural difference.** tau2 introduces a **second LLM — a user simulator** that plays
the customer. So each task involves two models talking to each other, plus tool calls. That single
fact explains most of tau2's profile: ~10 LLM calls and ~11 tool calls per task, and ~69k input
tokens because the whole growing conversation is re-sent on every turn.

**What it stresses.** Conversation state, tool selection over many turns, and the agent's ability to
stay on task. It is also the first benchmark where **model choice dominates the result** — see below.

**What to expect.** ~0.83 pass rate with claude-sonnet-5, ~92s median per task (some tasks 7×
that). Results are inherently a little noisy run-to-run because episodes are nondeterministic; ±0.05
across a 10–20 task run is normal variance, not a regression.

> **Model sensitivity is dramatic here.** On the same 10 tasks, tau2 scored **0.1 with gpt-5-mini**
> and **0.9 with claude-sonnet-5**. That is not a small tuning difference — it is the benchmark being
> effectively unsolvable for one model and mostly solved by another. The Service therefore pins tau2
> to claude-sonnet-5 via a `model_override`. If you see a tau2 number, check which model produced it
> before comparing anything.

---

## appworld — the hard one

**What it is.** AppWorld tasks are realistic everyday digital chores carried out across a suite of
simulated everyday apps via their APIs — the kind of request that requires discovering the right
APIs, chaining many calls, and handling intermediate state.

**Task ids look different.** appworld groups several related tasks under one base scenario/"world",
so ids look like `21abae1_1`, `21abae1_2`, `21abae1_3` — a scenario hash plus a sub-task number. Each
still runs as an independent session.

**What it stresses.** Long-horizon planning and composition. ~21 LLM calls and ~14 tool calls per
task, ~184k input tokens, and a median of **4 minutes per task**.

**Expect a pass rate of 0.0 — and that is the honest result.** We run appworld with a *generic*
`tool_calling` agent, which is not specialised for it. 0.0 does not mean the platform is broken; the
runs complete cleanly, tasks execute, tokens are recorded, evaluation simply says the agent did not
accomplish the goal. appworld's role in our matrix is as a **stress test of the pipeline at scale**
(long tasks, big contexts, real timeouts) rather than a capability score we expect to move.

---

## How to read our reports

A few things that trip people up:

- **`pass_rate` = `evaluated_pass / total`.** A task that errors before evaluation counts as not
  passed, so a low pass rate can mean "failed the task" *or* "never got to be judged".
- **The `llm` column overcounts real LLM calls by one.** Each task issues an extra probe call
  (`max_tokens=1`) before the real work, and it is counted as a `chat` span. So `llm=2` on gsm8k
  usually means *one* real call, and the gsm8k figure of 2.1 above is really ~1.1 real calls. Same
  offset applies to tau2 and appworld.
- **Input tokens grow faster than output.** Every LLM call re-sends the whole conversation, so
  cumulative input scales roughly with the square of the turn count while output is bounded per
  call. That is why tau2/appworld input dwarfs output, and why input-token variance between tasks is
  always wider than output-token variance.
- **Task selection is deterministic.** A run takes the first `max_tasks` tasks, so the same
  `task_id` is the same task across runs and clusters, and a smaller run's tasks are a prefix of a
  larger one's. Cross-run comparisons on the same benchmark are therefore like-for-like.
- **Implausibly small token counts are a telemetry bug, not a cheap task — and `== 0` is the wrong
  test.** When a task's usage-bearing span is lost, what survives is the `max_tokens=1` probe, and
  the probe's *own* usage depends on the model: a reasoning model (gpt-5-mini) has the probe
  rejected and records nothing (`in=0, out=0` — the familiar "zero-token row"), but
  claude-sonnet-5 accepts it and leaves `in=8, out=1`, and gemini-2.5-pro leaves `in=1, out=0`.
  Those are *non-zero*, so a zero-check misses every tau2 and appworld case. Use the structural
  test instead: **`llm` ≤ 1 alongside `tool` ≥ 2 is impossible**, since each tool call needs a
  preceding model turn. `llm=1` next to `tool=11` is a lost span. Affected runs' **token totals are
  understated while their pass rates remain valid** — the tasks really ran and were really judged.
  Cause and fix (fresh deploy per run, no warm-agent reuse):
  `docs/exgentic-agent-bug-report-20260901.md`.

## Picking a benchmark

| If you want to… | use |
|---|---|
| check a cluster/deploy/auth/telemetry path works | **gsm8k**, 1–10 tasks |
| exercise concurrency and volume cheaply | **gsm8k**, 50 tasks at `max_parallel_sessions=4` |
| compare models meaningfully | **tau2** (it discriminates; gsm8k saturates at ~1.0) |
| stress long contexts, long tasks, timeouts | **appworld** |
| get a fast signal that nothing regressed | **gsm8k** — if it fails, stop and fix infrastructure |

# The three benchmarks, for people new to them

We run three benchmarks through the Benchmarking Service: **gsm8k**, **tau2**, and **appworld**.
They are not three interchangeable test suites — they form a deliberate difficulty ladder, and each
one stresses a different part of the agent stack. This note explains what each is, what a single
task actually involves, and what they look like in practice.

All the "measured" figures below come from our own v1.23 runs (272 tasks across two clusters:
OpenShift `ykt3→ykt2` and single-node KinD), not from the benchmarks' published papers.

---

## At a glance

| | **gsm8k** | **tau2** | **appworld** |
|---|---|---|---|
| What it tests | multi-step arithmetic reasoning | multi-turn dialogue + tool use | long-horizon app automation |
| Tasks measured | 172 | 60 | 40 |
| **Pass rate** | **0.98** | **0.83** | **0.00** |
| Input tokens / task | **343** | **69,139** | **184,443** |
| Output tokens / task | 205 | 1,629 | 18,811 |
| LLM calls / task | 2.1 | 9.7 | 20.8 |
| Tool calls / task | 1.1 | 11.3 | 13.6 |
| Median task latency | **5.3s** | **92s** | **242s** |
| Slowest task seen | 62s | 425s | 581s |
| Model we use | gpt-5-mini (or gpt-4.1) | claude-sonnet-5 | gemini-2.5-pro |
| `task_id` format | integer (`0`, `1`, …) | integer (`0`, `1`, …) | `21abae1_1` |

The headline is the **scale gap**: a tau2 task costs ~200× the input tokens of a gsm8k task, and an
appworld task ~540×. Choose accordingly — a 50-task gsm8k run is a couple of minutes; a 20-task
appworld run is half an hour and millions of tokens.

---

## gsm8k — the smoke test

**What it is.** GSM8K ("Grade School Math 8K") is a well-known set of grade-school maths word
problems that need several arithmetic steps. Each problem has one correct numeric answer.

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
- **`0` tokens with a nonzero `llm` count is a telemetry bug, not a cheap task.** See
  `docs/exgentic-agent-bug-report-20260901.md`. If you see it, the run's token numbers are
  understated and should not be used.

## Picking a benchmark

| If you want to… | use |
|---|---|
| check a cluster/deploy/auth/telemetry path works | **gsm8k**, 1–10 tasks |
| exercise concurrency and volume cheaply | **gsm8k**, 50 tasks at `max_parallel_sessions=4` |
| compare models meaningfully | **tau2** (it discriminates; gsm8k saturates at ~1.0) |
| stress long contexts, long tasks, timeouts | **appworld** |
| get a fast signal that nothing regressed | **gsm8k** — if it fails, stop and fix infrastructure |

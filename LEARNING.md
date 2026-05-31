# LEARNING.md — How Ship-It works (an agentic-coding walkthrough)

A guided tour of how this codebase is put together, aimed at someone who already
knows **LangGraph / CrewAI / OpenAI Agents SDK** and wants to understand how
*this* multi-agent system fires up agents and runs them autonomously.

Every claim below links to the exact `file:line` so you can read the source as
you go. Paths are relative to the repo root; line numbers were accurate at the
time of writing (`backend/app/...`).

Companion docs: [`CLAUDE.md`](CLAUDE.md) (working conventions + SDK gotchas) and
[`SHIP-IT_BUILD_PLAN.md`](SHIP-IT_BUILD_PLAN.md) (the design source-of-truth).

---

## 0. The one idea that makes everything click

**The orchestrator is plain Python, not an LLM.** No model decides "what agent
runs next." A hand-written `async def run()` does. Each agent is just *one
`query()` call* with a role baked into its system prompt, and ordinary Python
statements/loops sequence them.

The design doc's phrasing, quoted in the orchestrator module docstring
([`backend/app/orchestrator.py:12`](backend/app/orchestrator.py)): *"the
orchestrator is a constrained state machine, not a free router."*

The payoff: gating, retries, persistence, and cost tracking are all trivial
Python — no prompt-engineering a manager agent to behave.

### Mapping to frameworks you know

| Concept | LangGraph | CrewAI | OpenAI Agents SDK | **Ship-It** |
|---|---|---|---|---|
| Who decides the next step | `StateGraph` edges (can be LLM-routed) | `Process` + manager agent | `handoffs` (LLM-chosen) | **Python `if`/`for` in `Orchestrator.run`** |
| A "node" / "agent" | graph node | crew member | an `Agent` | **one `query()` = `_run_stage(...)`** |
| Shared state | `State` TypedDict | crew context | run context | **`RunOutcome` dataclass + dicts passed by hand** |
| Agent → agent handoff | edge | delegation | handoff/tool | **Python parses output A, builds the prompt for B** |
| Observability | callbacks / stream | callbacks | tracing | **`EventBus` → CLI + SQLite + SSE** |
| Human-in-the-loop | interrupt / checkpoint | — | — | **`asyncio.Future` gate broker** |

---

## 1. How one agent "fires up" and works autonomously

The whole engine is **`_run_stage()`**
([`backend/app/orchestrator.py:91`](backend/app/orchestrator.py)). Stripped down
to its core ([`:104`](backend/app/orchestrator.py)):

```python
async for message in query(prompt=prompt, options=options):
    if isinstance(message, AssistantMessage):   # model spoke / called a tool
    elif isinstance(message, UserMessage):       # a tool returned a result
    elif isinstance(message, ResultMessage):     # run finished: cost, #turns
```

That `query()` is where the autonomy lives. Inside a **single** call, the Claude
Agent SDK runs the agent loop *for you*: the model thinks → decides to call a
tool (`Read`/`Write`/`Edit`/`Bash`) → sees the result → thinks again → … →
stops when the task is done or it hits `max_turns`. Your Python code does **not**
drive that inner loop — it just consumes the stream of messages and turns each
one into a `PipelineEvent` ([`:99-157`](backend/app/orchestrator.py)).

So there are **two loops**, and keeping them straight is the key insight:

- **Inner loop (autonomous, model-driven):** inside one `query()` — the SDK's
  think/act/observe cycle. Bounded by `max_turns`
  (Coder = 40 [`coder.py:72`](backend/app/agents/coder.py), Reviewer = 25
  [`reviewer.py:96`](backend/app/agents/reviewer.py), Planner = 6
  [`planner.py:62`](backend/app/agents/planner.py)).
- **Outer loop (deterministic, Python-driven):** `Orchestrator.run` sequencing
  stages ([`orchestrator.py:226`](backend/app/orchestrator.py)), plus the capped
  Coder↔Reviewer loop ([`:499`](backend/app/orchestrator.py)). Bounded by
  `max_review_rounds = 3` ([`:189`](backend/app/orchestrator.py)).

### `StageResult` — what one stage hands back to Python

Each `_run_stage` returns a `StageResult`
([`orchestrator.py:75-88`](backend/app/orchestrator.py)): the concatenated text
the model produced (which Python will parse for the spec / verdict / URL), plus
`cost_usd` and `turns` lifted from the SDK's `ResultMessage`
([`:143-147`](backend/app/orchestrator.py)).

---

## 2. The pipeline as a story — read `run()` top to bottom

`Orchestrator.run` ([`orchestrator.py:226-345`](backend/app/orchestrator.py)) is
the entire state machine. Read it as a narrative:

1. **Planner** → JSON spec — `_stage_planner` ([`:349`](backend/app/orchestrator.py))
2. **spec gate** (human) — `_gate("spec", …)` ([`:260`](backend/app/orchestrator.py));
   the human's notes ride into the Coder's first brief ([`:261`](backend/app/orchestrator.py))
3. **Scaffolder** → fresh Next.js project — `_stage_scaffolder` ([`:263`, `:367`](backend/app/orchestrator.py))
4. **Coder** (initial) → writes `app/page.tsx` — `_stage_coder_initial` ([`:265`, `:389`](backend/app/orchestrator.py))
5. **Coder ↔ Reviewer loop** (capped) — `_coder_reviewer_loop` ([`:267`, `:499`](backend/app/orchestrator.py))
6. **code gate** (human) — review the green build + a `git diff` ([`:291`](backend/app/orchestrator.py))
7. **deploy gate → Deployer** — only when `config.deploy` is set ([`:293-304`](backend/app/orchestrator.py))

The terminal `status` strings (`built`, `deployed`, `failed_review`,
`rejected_at_<gate>`, `expired_at_<gate>`, `errored`) are all set right here in
Python — see [`:270-306`](backend/app/orchestrator.py) and the error handlers at
[`:308-318`](backend/app/orchestrator.py).

### Expected failures vs. bugs

`PipelineFailure` ([`:60`](backend/app/orchestrator.py)) is raised for *expected*
user-facing failures (planner returned no valid spec, scaffolder produced no
project). The outer handler records it and returns cleanly
([`:308-314`](backend/app/orchestrator.py)). *Unexpected* exceptions are recorded
**and re-raised** ([`:315-318`, `:343-344`](backend/app/orchestrator.py)) so real
bugs still surface a traceback. This split is worth copying in your own systems.

---

## 3. What configures an agent: capabilities (hard) vs. behavior (soft)

Each `backend/app/agents/*.py` is a **factory**. The three core agents differ in
exactly the way that teaches the lesson:

| Agent | `allowed_tools` | Enforcement | Output contract |
|---|---|---|---|
| **Planner** | `[]` (think-only) — [`planner.py:44`](backend/app/agents/planner.py) | **Hard** — literally cannot touch disk | fenced ` ```json ` spec |
| **Coder** | `Read, Write, Edit, Bash` + `cwd` — [`coder.py:53`,`:78`](backend/app/agents/coder.py) | **Soft** — prompt says "only `app/`+`components/`, never `package.json`" ([`coder.py:36-39`](backend/app/agents/coder.py)) | one-line summary |
| **Reviewer** | `Read, Bash` (no Write/Edit) — [`reviewer.py:78`](backend/app/agents/reviewer.py) | **Hard** — *physically* can't fix, only observe | fenced ` ```json ` verdict |

**The lesson:** `allowed_tools` is your hard safety boundary; the system prompt is
soft. The Reviewer is read-only because `Write`/`Edit` are *absent from its tool
list* — not because you politely asked. If you want a guarantee, encode it in
tools; if you want a preference, put it in the prompt.

Two knobs that make agents run **unattended**
([`coder.py:71-83`](backend/app/agents/coder.py)):

- `permission_mode="acceptEdits"` — don't prompt a human for each file write.
- `cwd=str(workspace)` — sandbox the agent's `Bash`/`Write` to a per-run
  directory. Since `Bash` can run anything, you point `cwd` somewhere disposable
  (`workspaces/run-…`, created by
  `default_workspace_for_run` [`orchestrator.py:582`](backend/app/orchestrator.py)).

---

## 4. Context isolation — the trap to internalize

Each stage's `query()` is a **brand-new conversation**. The Coder cannot see the
Planner's reasoning. It sees *only* the brief the orchestrator hands it. That's
why `build_coder_brief()`
([`coder.py:86`](backend/app/agents/coder.py)) literally serializes the spec JSON
into the Coder's user prompt ([`coder.py:108-111`](backend/app/agents/coder.py)):

```python
"Spec (verbatim):\n\n```json\n" + json.dumps(spec, indent=2) + "\n```\n"
```

Mental model: this is exactly like passing explicit state between LangGraph
nodes — there is **no shared scratchpad**. Whatever a worker needs, you must pack
into its brief. (CLAUDE.md calls this the #1 thing to respect.)

---

## 5. The autonomous self-correction loop

`_coder_reviewer_loop` ([`orchestrator.py:499-521`](backend/app/orchestrator.py)):

```
for round in 1..max_review_rounds:
    verdict = reviewer()            # runs lint → tsc → build, returns JSON
    if verdict == "pass": return    # green → done
    if last round: break            # give up (escalate, don't fake success)
    coder_revise(issues)            # feed structured issues back to Coder
```

What makes this loop *work* is the **structured data contract at the seam**: the
Reviewer emits `issues: [{tool, summary, evidence}]`
([`reviewer.py:55-67`](backend/app/agents/reviewer.py)), and
`build_coder_revision_brief()`
([`coder.py:115`](backend/app/agents/coder.py)) renders those issues into the
next Coder prompt. So "agent talks to agent" is really *"Python extracts a
structured artifact from agent A and templates it into the prompt for agent B."*

Two pieces of discipline here:

- If the Reviewer's JSON won't parse, the loop **aborts** rather than feeding a
  garbage "issue" back to the Coder and burning model calls
  ([`orchestrator.py:506-515`](backend/app/orchestrator.py)).
- The loop is **capped**, and on hitting the cap the run ends `failed_review`
  rather than pretending success ([`:270-274`, `:518-519`](backend/app/orchestrator.py)).
  (A core safety rail from CLAUDE.md: always cap loops; never ship un-approved.)

---

## 6. Structured I/O without a schema enforcer

Models emit prose; Python needs structure. The pattern repeats for
Planner / Reviewer / Deployer:

1. **Prompt demands** exactly one fenced ` ```json ` block
   ([`planner.py:26-42`](backend/app/agents/planner.py)).
2. **Regex extracts** it — `_JSON_BLOCK_RE`
   ([`planner.py:85`](backend/app/agents/planner.py),
   [`reviewer.py:110`](backend/app/agents/reviewer.py)).
3. **`json.loads`** parses; **defensive fallback** if it fails: the Reviewer
   returns a synthetic `fail` verdict tagged `_parse_error` instead of throwing
   ([`reviewer.py:113-150`](backend/app/agents/reviewer.py)).

This is "structured output by convention + parsing." The SDK *can* force
tool-based structured output, but this project chose prompt-contract + regex —
simpler, and every failure mode is visible.

---

## 7. Observability — one run, three consumers

**`EventBus`** ([`backend/app/events.py:45`](backend/app/events.py)) is a tiny
pub/sub. `_run_stage` turns every model message into a `PipelineEvent`
([`events.py:33`](backend/app/events.py)) and `emit()`
([`events.py:72`](backend/app/events.py)) fans it to every listener:

- the **CLI printer** (Milestone 1),
- the **SQLite recorder** — attached per-run and detached in `finally`
  ([`orchestrator.py:238`, `:341`](backend/app/orchestrator.py)) so a long-lived
  bus doesn't accumulate stale recorders (see the warning at
  [`events.py:59-66`](backend/app/events.py)),
- the **dashboard SSE stream** (Milestone 2).

Same run, three consumers, zero coupling. This is the project's equivalent of
LangGraph streaming / callbacks, hand-rolled in ~30 lines. Listeners may be sync
or async; the bus awaits coroutines ([`events.py:72-77`](backend/app/events.py)).

The full event vocabulary is the `EventKind` literal
([`events.py:17-30`](backend/app/events.py)).

---

## 8. Human-in-the-loop gates

To pause an otherwise-autonomous pipeline for a human, `_gate()`
([`orchestrator.py:402-468`](backend/app/orchestrator.py)) emits a `gate_open`
event and then **`await`s an `asyncio.Future`**
([`:420`, `:430`](backend/app/orchestrator.py)). The FastAPI endpoint resolves
that Future when you click approve/reject. No thread blocks — it's pure asyncio.

The broker itself is small: `GateBroker`
([`backend/app/gates.py:28`](backend/app/gates.py)) maps `(run_id, name)` →
`Future[GateDecision]`, with `open()` ([`gates.py:34`](backend/app/gates.py)),
`resolve()` ([`gates.py:46`](backend/app/gates.py)), and `cancel_all()`
([`gates.py:56`](backend/app/gates.py), used by the cancel endpoint).

Two details worth studying:

- **Register-the-future-before-emitting** ([`orchestrator.py:415-420`](backend/app/orchestrator.py)) —
  otherwise a fast dashboard could POST a decision before the future exists and
  get a 404.
- **Timeout** ([`:430-450`](backend/app/orchestrator.py)) — an abandoned browser
  tab must not leak the orchestrator task forever; on timeout the run ends
  `expired_at_<gate>`.

**Documented limitation:** the broker is in-memory, so a server restart orphans
paused runs at `running` (the Milestone 4 TODO — see
[`gates.py:7-13`](backend/app/gates.py)).

When gates aren't configured (the CLI path), `_gate()` short-circuits to
auto-approve ([`orchestrator.py:413-414`](backend/app/orchestrator.py)), so M1/M2
behaviour is unchanged.

---

## 9. The subtlety: every agent file defines *two* builders

`build_planner()` → `AgentDefinition`
([`planner.py:47`](backend/app/agents/planner.py)) **and**
`build_planner_options()` → `ClaudeAgentOptions`
([`planner.py:61`](backend/app/agents/planner.py)). These are the **two ways to
compose agents** in the Claude Agent SDK:

- **`AgentDefinition` + the `Agent` tool** = model-driven sub-agents (the *model*
  decides to delegate — closest to CrewAI hierarchical / OpenAI handoffs). This
  requires `"Agent"` to be in `allowed_tools`, or delegation silently never
  happens — the loud gotcha in CLAUDE.md.
- **`ClaudeAgentOptions` (with `system_prompt`) + your own `query()`** = each role
  is the *main* agent of its own isolated call, sequenced by Python.

**This pipeline uses the second exclusively** — see the orchestrator's imports,
every one a `_options` factory
([`orchestrator.py:43-54`](backend/app/orchestrator.py)). The `AgentDefinition`s
exist for the alternative composition style.

Understanding *why* they chose the Python-driven style over Agent-tool delegation
is the deepest lesson here: **determinism, isolation, and easy gating** beat
letting a model route, for a pipeline like this.

---

## 10. Suggested reading path

Read in this order — simplest first, each building on the last:

1. [`backend/app/events.py`](backend/app/events.py) — `PipelineEvent` + `EventBus`. Everything emits into it.
2. [`backend/app/agents/planner.py`](backend/app/agents/planner.py) — the simplest agent: `tools=[]`, prompt contract, regex parse, both builders.
3. [`backend/app/orchestrator.py:91`](backend/app/orchestrator.py) (`_run_stage`) — the universal "run one agent" primitive.
4. [`backend/app/orchestrator.py:226`](backend/app/orchestrator.py) (`run`) — the whole state machine as one method.
5. [`backend/app/agents/reviewer.py`](backend/app/agents/reviewer.py) + [`coder.py`](backend/app/agents/coder.py) — compare tool lists (hard vs soft) and the two coder briefs.
6. [`backend/app/orchestrator.py:499`](backend/app/orchestrator.py) (`_coder_reviewer_loop`) — the capped self-correction loop.
7. [`backend/app/gates.py`](backend/app/gates.py) + `_gate()` — the asyncio human-in-the-loop pattern.

### Exercise to cement it

Trace a single run by hand for the idea **"a tip calculator"**:

1. What `spec` dict does the Planner return?
2. What string does `build_coder_brief` produce from it?
3. Which tools does the Coder call, in what order?
4. What `verdict` JSON does the Reviewer emit?
5. Which `PipelineEvent`s fire, in order, from `pipeline_start` to `pipeline_end`?

Then start a real run on the dashboard (`http://localhost:3000`) and watch the
activity stream confirm — or correct — your trace.

---

## Appendix — file map

| File | Role |
|---|---|
| [`backend/app/orchestrator.py`](backend/app/orchestrator.py) | The Python state machine. `_run_stage` (run one agent) + `run` (sequence them) + the capped review loop + gates. |
| [`backend/app/agents/planner.py`](backend/app/agents/planner.py) | Think-only agent: idea → JSON spec. |
| [`backend/app/agents/scaffolder.py`](backend/app/agents/scaffolder.py) | Read/Write/Edit/Bash: fresh Next.js project + initial git commit. |
| [`backend/app/agents/coder.py`](backend/app/agents/coder.py) | Read/Write/Edit/Bash: spec → `app/page.tsx` (initial + revision briefs). |
| [`backend/app/agents/reviewer.py`](backend/app/agents/reviewer.py) | Read/Bash (read-only): lint/tsc/build → structured verdict. |
| [`backend/app/agents/deployer.py`](backend/app/agents/deployer.py) | Read/Bash: `vercel deploy --prod` → URL. |
| [`backend/app/events.py`](backend/app/events.py) | `PipelineEvent` + `EventBus` pub/sub. |
| [`backend/app/gates.py`](backend/app/gates.py) | `GateBroker` + `GateDecision` (human-in-the-loop). |
| [`backend/app/store.py`](backend/app/store.py) | SQLite persistence: `runs` + `events`. |
| [`backend/app/server.py`](backend/app/server.py) | FastAPI: `/api/runs`, SSE event stream, gate endpoints. |
| [`backend/app/run_spike.py`](backend/app/run_spike.py) | CLI entrypoint (Milestones 0–1). |

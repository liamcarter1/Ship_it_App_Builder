"""Milestone 1 orchestrator — a Python-driven state machine.

The pipeline:

    Planner -> Scaffolder -> Coder -> Reviewer ↺ ... -> Deployer (optional)
                                       ↑       │
                                       └───────┘  (green-build loop, capped)

Each stage opens its own `query()` against the Claude Agent SDK with that
role as the **main agent** (its prompt is the `system_prompt`). No
model-driven Agent-tool delegation here: keeping the state machine in Python
makes gating, retries, and persistence straightforward, and matches the
design doc's "the orchestrator is a constrained state machine, not a free
router" stance.

Every message we see is converted into a `PipelineEvent` and fanned out via
the `EventBus`. The CLI subscribes a pretty-printer; the SQLite `Store`
subscribes a recorder; in Milestone 2 the dashboard will subscribe an SSE
publisher.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from .agents import (
    build_coder_brief,
    build_coder_options,
    build_coder_revision_brief,
    build_deployer_options,
    build_planner_options,
    build_reviewer_options,
    build_scaffolder_options,
    parse_deployer_result,
    parse_planner_spec,
    parse_reviewer_verdict,
)
from .events import EventBus, PipelineEvent
from .gates import GateBroker, GateDecision
from .store import Store, attach_store_to_bus


class PipelineFailure(RuntimeError):
    """Expected, user-facing failure raised by a stage.

    The orchestrator records these as `outcome.error` + the right
    `outcome.status` and lets `run()` return normally, so the CLI can print a
    clean summary instead of a traceback. Unexpected exceptions (programmer
    bugs) still propagate.
    """


# ---------------------------------------------------------------------------
# Stage runner
# ---------------------------------------------------------------------------


@dataclass
class StageResult:
    """Everything one stage produces.

    `text` is the concatenation of the agent's TextBlocks (the model's "final
    answer" text we'll parse for spec/verdict/URL). `cost_usd` and `turns`
    come from the SDK's `ResultMessage`.
    """

    text: str = ""
    cost_usd: Optional[float] = None
    turns: Optional[int] = None
    is_error: bool = False
    raw_result: Optional[str] = None


async def _run_stage(
    *,
    stage: str,
    prompt: str,
    options: ClaudeAgentOptions,
    bus: EventBus,
) -> StageResult:
    """Run one stage to completion, streaming events into the bus."""
    await bus.emit(PipelineEvent(kind="stage_start", source=stage))

    result = StageResult()
    text_parts: list[str] = []

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)
                    await bus.emit(
                        PipelineEvent(kind="agent_text", source=stage, text=block.text)
                    )
                elif isinstance(block, ToolUseBlock):
                    await bus.emit(
                        PipelineEvent(
                            kind="tool_use",
                            source=stage,
                            text=block.name,
                            meta={"tool_use_id": block.id, "input": block.input},
                        )
                    )
        elif isinstance(message, UserMessage):
            for block in getattr(message, "content", []) or []:
                if isinstance(block, ToolResultBlock):
                    await bus.emit(
                        PipelineEvent(
                            kind="tool_result",
                            source="tool",
                            text=str(block.content)[:500],
                            meta={
                                "tool_use_id": block.tool_use_id,
                                "is_error": bool(block.is_error),
                            },
                        )
                    )
        elif isinstance(message, SystemMessage):
            await bus.emit(
                PipelineEvent(
                    kind="system",
                    source="system",
                    text=str(getattr(message, "subtype", "system")),
                )
            )
        elif isinstance(message, ResultMessage):
            result.cost_usd = getattr(message, "total_cost_usd", None)
            result.turns = getattr(message, "num_turns", None)
            result.is_error = bool(getattr(message, "is_error", False))
            result.raw_result = getattr(message, "result", None)

    result.text = "\n".join(text_parts).strip()
    await bus.emit(
        PipelineEvent(
            kind="stage_end",
            source=stage,
            text="error" if result.is_error else "ok",
            meta={"cost_usd": result.cost_usd, "turns": result.turns},
        )
    )
    return result


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


@dataclass
class RunOutcome:
    run_id: int
    idea: str
    workspace: Path
    spec: Optional[dict] = None
    review_rounds: int = 0
    last_verdict: Optional[dict] = None
    deploy: Optional[dict] = None
    total_cost_usd: float = 0.0
    page_tsx_written: bool = False
    status: str = "running"
    error: Optional[str] = None
    per_stage_cost: dict[str, float] = field(default_factory=dict)


@dataclass
class OrchestratorConfig:
    planner_model: Optional[str] = None
    scaffolder_model: Optional[str] = None
    coder_model: Optional[str] = None
    reviewer_model: Optional[str] = None
    deployer_model: Optional[str] = None
    max_review_rounds: int = 3
    deploy: bool = False  # default off — Vercel auth not assumed
    # Approval gates (Milestone 3). When `gate_broker` is None or a given
    # stage isn't in `gated_stages`, `_gate()` auto-approves silently. The
    # CLI keeps both at defaults so M1/M2 behaviour is unchanged; the
    # FastAPI server passes a shared broker and the full set.
    gate_broker: Optional[GateBroker] = None
    gated_stages: tuple[str, ...] = ()
    # Bound how long a gate may wait for a human. Without this, an abandoned
    # browser tab would leak the orchestrator's asyncio task forever. On
    # timeout the run ends with `expired_at_<gate>`.
    gate_timeout_s: float = 3600.0


class Orchestrator:
    """Drives one pipeline run end-to-end."""

    def __init__(
        self,
        *,
        bus: EventBus,
        store: Store,
        config: OrchestratorConfig | None = None,
    ) -> None:
        self.bus = bus
        self.store = store
        self.config = config or OrchestratorConfig()

    # --- helpers -----------------------------------------------------------

    def _add_cost(self, outcome: RunOutcome, stage: str, result: StageResult) -> None:
        if result.cost_usd is not None:
            outcome.per_stage_cost[stage] = result.cost_usd
            outcome.total_cost_usd += result.cost_usd

    # --- main entrypoint ---------------------------------------------------

    async def run(self, idea: str, *, workspace: Optional[Path] = None, run_id: Optional[int] = None) -> RunOutcome:
        workspace = workspace or default_workspace_for_run()
        workspace.mkdir(parents=True, exist_ok=True)

        # The API server creates the run row up-front so it can return the
        # run_id to the client before kicking off the pipeline (so the
        # dashboard can open the SSE stream immediately). The CLI path
        # passes run_id=None and we create the row here.
        if run_id is None:
            run_id = self.store.create_run(idea=idea, workspace=workspace)
        # Per-run listener: attach now, detach in `finally` so reusing this
        # Orchestrator across runs doesn't accumulate stale recorders.
        store_listener = attach_store_to_bus(self.bus, self.store, run_id)

        outcome = RunOutcome(run_id=run_id, idea=idea, workspace=workspace)

        await self.bus.emit(
            PipelineEvent(
                kind="pipeline_start",
                source="orchestrator",
                text=f"run #{run_id}: {idea!r}",
                meta={"workspace": str(workspace), "run_id": run_id},
            )
        )

        unexpected: Optional[BaseException] = None
        try:
            try:
                spec = await self._stage_planner(idea, outcome)
                outcome.spec = spec

                # Spec gate — the cheapest place to course-correct: the
                # human reviews the JSON spec before any code is written.
                # Notes (if any) ride along into the Coder's first brief.
                spec_decision = await self._gate("spec", {"spec": spec}, outcome)
                spec_notes = spec_decision.notes if spec_decision else None

                await self._stage_scaffolder(workspace, outcome)

                await self._stage_coder_initial(spec, workspace, outcome, gate_notes=spec_notes)

                verdict = await self._coder_reviewer_loop(spec, workspace, outcome)
                outcome.last_verdict = verdict

                if verdict.get("verdict") != "pass":
                    outcome.status = "failed_review"
                    outcome.error = (
                        f"review still failing after {outcome.review_rounds} rounds"
                    )
                else:
                    page_tsx = workspace / "app" / "page.tsx"
                    outcome.page_tsx_written = page_tsx.exists()

                    # Code gate — review the green build + reviewer verdict
                    # before going further. Always fires (deploy or no), so
                    # CLI/'built' runs get a final approval too.
                    await self._gate(
                        "code",
                        {"workspace": str(workspace), "verdict": verdict},
                        outcome,
                    )

                    if self.config.deploy:
                        # Deploy gate — last chance before pushing to Vercel.
                        await self._gate(
                            "deploy",
                            {"workspace": str(workspace)},
                            outcome,
                        )
                        deploy = await self._stage_deployer(workspace, outcome)
                        outcome.deploy = deploy
                        outcome.status = (
                            "deployed" if deploy.get("status") == "deployed" else "deploy_failed"
                        )
                    else:
                        outcome.status = "built"

            except PipelineFailure as exc:
                # Expected, user-facing failure (e.g. Planner couldn't return a
                # valid spec, Scaffolder couldn't produce a project). Record it
                # and return cleanly — the CLI prints the summary from
                # outcome.error rather than a Python traceback.
                outcome.status = outcome.status if outcome.status != "running" else "errored"
                outcome.error = str(exc)
            except Exception as exc:  # programmer error / unknown — surface
                outcome.status = "errored"
                outcome.error = f"{type(exc).__name__}: {exc}"
                unexpected = exc

            await self.bus.emit(
                PipelineEvent(
                    kind="pipeline_end",
                    source="orchestrator",
                    text=outcome.error or outcome.status,
                    meta={
                        "is_error": outcome.status not in ("built", "deployed"),
                        "total_cost_usd": outcome.total_cost_usd,
                        "review_rounds": outcome.review_rounds,
                        "deploy_url": (outcome.deploy or {}).get("url"),
                    },
                )
            )
            self.store.finish_run(
                run_id,
                status=outcome.status,
                total_cost_usd=outcome.total_cost_usd,
                deploy_url=(outcome.deploy or {}).get("url"),
                error=outcome.error,
            )
        finally:
            self.bus.remove(store_listener)

        if unexpected is not None:
            raise unexpected
        return outcome

    # --- stage implementations --------------------------------------------

    async def _stage_planner(self, idea: str, outcome: RunOutcome) -> dict:
        options = build_planner_options(model=self.config.planner_model)
        prompt = (
            "Idea: " + idea.strip() + "\n\n"
            "Return the JSON spec exactly as described in your system prompt."
        )
        result = await _run_stage(stage="planner", prompt=prompt, options=options, bus=self.bus)
        self._add_cost(outcome, "planner", result)
        try:
            spec = parse_planner_spec(result.text)
        except ValueError as exc:
            # ValueError covers both "no JSON block" and json.JSONDecodeError.
            outcome.status = "failed_planner"
            raise PipelineFailure(
                f"planner did not return a valid JSON spec: {exc}"
            ) from exc
        return spec

    async def _stage_scaffolder(self, workspace: Path, outcome: RunOutcome) -> None:
        options = build_scaffolder_options(workspace, model=self.config.scaffolder_model)
        prompt = (
            "Scaffold a fresh Next.js + TS + Tailwind project in the current "
            "working directory, following your system prompt. Stop when done."
        )
        result = await _run_stage(stage="scaffolder", prompt=prompt, options=options, bus=self.bus)
        self._add_cost(outcome, "scaffolder", result)

        # Sanity-check the scaffolder actually produced a project.
        for needed in ("package.json", "tsconfig.json", "app/page.tsx", "app/layout.tsx"):
            if not (workspace / needed).exists():
                outcome.status = "failed_scaffold"
                raise PipelineFailure(
                    f"scaffolder did not produce {needed}; saw: {result.text[-300:]}"
                )
        if "SCAFFOLD_FAILED" in result.text:
            outcome.status = "failed_scaffold"
            raise PipelineFailure(
                f"scaffolder reported failure: {result.text[-300:]}"
            )

    async def _stage_coder_initial(
        self,
        spec: dict,
        workspace: Path,
        outcome: RunOutcome,
        *,
        gate_notes: Optional[str] = None,
    ) -> None:
        options = build_coder_options(workspace, model=self.config.coder_model)
        prompt = build_coder_brief(spec, gate_notes=gate_notes)
        result = await _run_stage(stage="coder", prompt=prompt, options=options, bus=self.bus)
        self._add_cost(outcome, "coder", result)

    async def _gate(
        self, name: str, payload: dict, outcome: RunOutcome
    ) -> Optional[GateDecision]:
        """Pause for human approval at `name` if configured; otherwise
        return None and continue silently.

        Emits `gate_open` (the dashboard renders an approve/reject panel
        from this event) and `gate_decision` (the dashboard dismisses the
        panel). A rejection raises PipelineFailure, which the outer error
        handler in `run()` records as `outcome.status = rejected_at_<name>`.
        """
        if name not in self.config.gated_stages or self.config.gate_broker is None:
            return None
        # Register the future FIRST, then emit the event. If we emitted first
        # and the dashboard POSTed a resolve before we got back to register,
        # the API would 404. The current bus listeners are sync so emit
        # doesn't actually yield today, but doing it in this order means we
        # stay safe when M4 adds an async listener (e.g. WebSocket push).
        future = self.config.gate_broker.open(outcome.run_id, name)
        await self.bus.emit(
            PipelineEvent(
                kind="gate_open",
                source="orchestrator",
                text=name,
                meta={"name": name, "payload": payload},
            )
        )
        try:
            decision = await asyncio.wait_for(future, timeout=self.config.gate_timeout_s)
        except asyncio.TimeoutError:
            # No human in the loop within the budget. End the run cleanly
            # with `expired_at_<name>` so abandoned tabs don't pile up
            # hanging orchestrator tasks indefinitely.
            outcome.status = f"expired_at_{name}"
            await self.bus.emit(
                PipelineEvent(
                    kind="gate_decision",
                    source="orchestrator",
                    text=name,
                    meta={
                        "name": name,
                        "approve": False,
                        "notes": f"timeout after {self.config.gate_timeout_s:g}s",
                    },
                )
            )
            raise PipelineFailure(
                f"gate '{name}' expired after {self.config.gate_timeout_s:g}s with no decision"
            )
        await self.bus.emit(
            PipelineEvent(
                kind="gate_decision",
                source="orchestrator",
                text=name,
                meta={
                    "name": name,
                    "approve": decision.approve,
                    "notes": decision.notes,
                },
            )
        )
        if not decision.approve:
            outcome.status = f"rejected_at_{name}"
            raise PipelineFailure(
                f"rejected at gate '{name}': {decision.notes or '(no notes)'}"
            )
        return decision

    async def _stage_reviewer(self, workspace: Path, outcome: RunOutcome) -> dict:
        options = build_reviewer_options(workspace, model=self.config.reviewer_model)
        prompt = (
            "Run the three checks in order, then return the JSON verdict "
            "exactly as your system prompt specifies."
        )
        result = await _run_stage(stage="reviewer", prompt=prompt, options=options, bus=self.bus)
        self._add_cost(outcome, "reviewer", result)
        verdict = parse_reviewer_verdict(result.text)
        await self.bus.emit(
            PipelineEvent(
                kind="review_verdict",
                source="reviewer",
                text=verdict.get("verdict", "?"),
                meta={"issues": verdict.get("issues", [])},
            )
        )
        return verdict

    async def _stage_coder_revise(
        self, spec: dict, verdict: dict, workspace: Path, round_num: int, outcome: RunOutcome
    ) -> None:
        options = build_coder_options(workspace, model=self.config.coder_model)
        prompt = build_coder_revision_brief(spec, verdict.get("issues", []), round_num)
        result = await _run_stage(
            stage=f"coder-r{round_num}", prompt=prompt, options=options, bus=self.bus
        )
        self._add_cost(outcome, f"coder-r{round_num}", result)

    async def _coder_reviewer_loop(
        self, spec: dict, workspace: Path, outcome: RunOutcome
    ) -> dict:
        verdict: dict = {"verdict": "fail", "issues": []}
        for round_num in range(1, self.config.max_review_rounds + 1):
            outcome.review_rounds = round_num
            verdict = await self._stage_reviewer(workspace, outcome)
            if verdict.get("_parse_error"):
                # The Reviewer's output couldn't be parsed; we have no real
                # build verdict to react to. Looping here would feed the
                # parse-error blob to the Coder as a fake "build issue" and
                # waste up to 2*max_review_rounds extra model calls per cap.
                outcome.status = "failed_review"
                raise PipelineFailure(
                    "reviewer did not return a parseable JSON verdict; "
                    "aborting before fake-issue revision loop"
                )
            if verdict.get("verdict") == "pass":
                return verdict
            if round_num == self.config.max_review_rounds:
                break
            await self._stage_coder_revise(spec, verdict, workspace, round_num, outcome)
        return verdict

    async def _stage_deployer(self, workspace: Path, outcome: RunOutcome) -> dict:
        options = build_deployer_options(workspace, model=self.config.deployer_model)
        prompt = (
            "Deploy this project to Vercel production per your system prompt "
            "and return the JSON result."
        )
        result = await _run_stage(stage="deployer", prompt=prompt, options=options, bus=self.bus)
        self._add_cost(outcome, "deployer", result)
        parsed = parse_deployer_result(result.text)
        if parsed.get("url"):
            await self.bus.emit(
                PipelineEvent(kind="deploy_url", source="deployer", text=parsed["url"])
            )
        return parsed


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


def default_workspace_root() -> Path:
    """`<repo>/workspaces`. Per-run subdirs live here."""
    return Path(
        os.environ.get(
            "SHIPIT_WORKSPACES",
            str(Path(__file__).resolve().parents[2] / "workspaces"),
        )
    )


def default_workspace_for_run() -> Path:
    """Fresh per-run workspace dir, used when the caller doesn't pass one."""
    return default_workspace_root() / f"run-{int(time.time())}-{uuid.uuid4().hex[:6]}"

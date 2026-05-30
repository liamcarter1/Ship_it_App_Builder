"""Milestone 1 CLI entrypoint.

    python -m app.run_spike                                    # default idea
    python -m app.run_spike "a yoga studio landing page"       # custom idea
    python -m app.run_spike --deploy "an indie note-taking app"
    python -m app.run_spike --max-rounds 2 "..."
    python -m app.run_spike --show-run 4                       # replay a past run

The pipeline runs Planner -> Scaffolder -> Coder -> (Reviewer ↺ Coder)* and,
with `--deploy`, ends with a Vercel deployment.

The transcript-independent pass/fail signal is:
- workspace contains `app/page.tsx`  (Scaffolder + Coder both wrote one)
- Reviewer's last verdict is `pass`  (the build was green)
- with `--deploy`, the Deployer returned a `vercel.app` URL
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .events import EventBus, PipelineEvent
from .orchestrator import Orchestrator, OrchestratorConfig
from .store import Store

DEFAULT_IDEA = "a calm one-page landing site for a tiny indie note-taking app"


# Compact ANSI labels so the CLI output is readable even without a TTY parser.
_LABEL_COLOURS = {
    "orchestrator": "\033[36m",  # cyan
    "planner": "\033[35m",       # magenta
    "scaffolder": "\033[34m",    # blue
    "coder": "\033[32m",         # green
    "reviewer": "\033[31m",      # red
    "deployer": "\033[95m",      # bright magenta
    "tool": "\033[33m",          # yellow
    "system": "\033[90m",        # bright black
}
_RESET = "\033[0m"


def _label_for(source: str) -> str:
    # Strip e.g. "coder-r2" suffix for colour lookup but keep it in display.
    base = source.split("-", 1)[0]
    colour = _LABEL_COLOURS.get(base, "")
    return f"{colour}[{source}]{_RESET}" if colour else f"[{source}]"


def _print_event(event: PipelineEvent) -> None:
    label = _label_for(event.source)
    if event.kind == "pipeline_start":
        print(f"{label} {event.text}")
    elif event.kind == "stage_start":
        print(f"{label} ── start ──")
    elif event.kind == "stage_end":
        cost = event.meta.get("cost_usd")
        turns = event.meta.get("turns")
        suffix = []
        if turns is not None:
            suffix.append(f"turns={turns}")
        if cost is not None:
            suffix.append(f"cost=${cost:.4f}")
        tail = f" ({', '.join(suffix)})" if suffix else ""
        print(f"{label} ── end {event.text}{tail} ──")
    elif event.kind == "agent_text":
        # Trim very long planner/coder summaries on stdout; the store keeps the full text.
        text = event.text.strip()
        if len(text) > 400:
            text = text[:400] + " ..."
        if text:
            print(f"{label} {text}")
    elif event.kind == "tool_use":
        print(f"{label} tool: {event.text}")
    elif event.kind == "tool_result":
        snippet = event.text.replace("\n", " ")
        if len(snippet) > 160:
            snippet = snippet[:160] + "..."
        marker = "ERR " if event.meta.get("is_error") else ""
        print(f"{label} result: {marker}{snippet}")
    elif event.kind == "review_verdict":
        issues = event.meta.get("issues", []) or []
        print(f"{label} VERDICT: {event.text}  (issues={len(issues)})")
        for issue in issues[:5]:
            print(f"{label}   - [{issue.get('tool', '?')}] {issue.get('summary', '')}")
    elif event.kind == "deploy_url":
        print(f"{label} LIVE URL: {event.text}")
    elif event.kind == "system":
        # System messages are noisy; print only their subtype label.
        print(f"{label} {event.text}")
    elif event.kind == "pipeline_end":
        cost = event.meta.get("total_cost_usd")
        rounds = event.meta.get("review_rounds")
        url = event.meta.get("deploy_url")
        suffix = []
        if rounds is not None:
            suffix.append(f"review_rounds={rounds}")
        if cost is not None:
            suffix.append(f"cost=${cost:.4f}")
        if url:
            suffix.append(f"url={url}")
        tail = f" ({', '.join(suffix)})" if suffix else ""
        print(f"{label} pipeline finished: {event.text}{tail}")


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run_spike", description="Run the Ship-It pipeline.")
    p.add_argument("idea", nargs="*", help="The one-line product idea (default: a note-taking app).")
    p.add_argument("--deploy", action="store_true", help="Deploy to Vercel after a green build.")
    p.add_argument("--max-rounds", type=int, default=3,
                   help="Max Coder<->Reviewer rounds (default 3).")
    p.add_argument("--planner-model", default=os.environ.get("PLANNER_MODEL") or None)
    p.add_argument("--scaffolder-model", default=os.environ.get("SCAFFOLDER_MODEL") or None)
    p.add_argument("--coder-model", default=os.environ.get("CODER_MODEL") or None)
    p.add_argument("--reviewer-model", default=os.environ.get("REVIEWER_MODEL") or None)
    p.add_argument("--deployer-model", default=os.environ.get("DEPLOYER_MODEL") or None)
    p.add_argument("--workspace", type=Path, default=None,
                   help="Override the workspace directory.")
    p.add_argument("--show-run", type=int, default=None,
                   help="Replay a past run's events from SQLite (no model calls).")
    p.add_argument("--list-runs", action="store_true",
                   help="List recent runs from SQLite.")
    return p


def _replay_run(store: Store, run_id: int) -> int:
    run = store.get_run(run_id)
    if run is None:
        print(f"no run with id {run_id}", file=sys.stderr)
        return 1
    print(f"# run #{run['id']} — status={run['status']} idea={run['idea']!r}")
    print(f"# workspace={run['workspace']} cost=${run['total_cost_usd'] or 0:.4f}")
    if run["deploy_url"]:
        print(f"# deploy_url={run['deploy_url']}")
    for ev in store.list_events(run_id):
        event = PipelineEvent(
            kind=ev["kind"], source=ev["source"], text=ev["text"] or "",
            meta=__import__("json").loads(ev["meta"] or "{}"), ts=ev["ts"],
        )
        _print_event(event)
    return 0


def _list_runs(store: Store) -> int:
    rows = store.list_runs()
    if not rows:
        print("(no runs)")
        return 0
    for r in rows:
        print(f"#{r['id']:>4}  {r['status']:<14}  ${(r['total_cost_usd'] or 0):.4f}  "
              f"{r['idea'][:70]}")
    return 0


async def _main_async(args: argparse.Namespace) -> int:
    idea = " ".join(args.idea).strip() or DEFAULT_IDEA

    bus = EventBus()
    bus.add(_print_event)  # store listener is added inside Orchestrator.run after run_id exists
    store = Store()

    config = OrchestratorConfig(
        planner_model=args.planner_model,
        scaffolder_model=args.scaffolder_model,
        coder_model=args.coder_model,
        reviewer_model=args.reviewer_model,
        deployer_model=args.deployer_model,
        max_review_rounds=args.max_rounds,
        deploy=args.deploy,
    )

    orchestrator = Orchestrator(bus=bus, store=store, config=config)
    outcome = await orchestrator.run(idea, workspace=args.workspace)

    print()
    print(f"workspace:       {outcome.workspace}")
    print(f"page.tsx exists: {outcome.page_tsx_written}")
    print(f"review rounds:   {outcome.review_rounds}")
    print(f"last verdict:    {(outcome.last_verdict or {}).get('verdict', '?')}")
    print(f"total cost:      ${outcome.total_cost_usd:.4f}")
    if outcome.deploy:
        print(f"deploy:          {outcome.deploy.get('status')}  {outcome.deploy.get('url') or ''}")
    print(f"status:          {outcome.status}")
    if outcome.error:
        print(f"error:           {outcome.error}")
        return 1

    # Pass/fail per the design doc: green build + page.tsx exists.
    last_verdict = (outcome.last_verdict or {}).get("verdict")
    if outcome.page_tsx_written and last_verdict == "pass":
        if args.deploy:
            return 0 if (outcome.deploy or {}).get("status") == "deployed" else 1
        return 0
    return 1


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()

    # Find the repo's .env so this works whether you run from `backend/` or anywhere else.
    here = Path(__file__).resolve()
    for candidate in (here.parents[1] / ".env", here.parents[2] / ".env"):
        if candidate.exists():
            load_dotenv(candidate)
            break
    else:
        load_dotenv()

    store = Store()

    if args.list_runs:
        sys.exit(_list_runs(store))
    if args.show_run is not None:
        sys.exit(_replay_run(store, args.show_run))

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "[warn] ANTHROPIC_API_KEY is not set. The SDK will fall back to a "
            "logged-in `claude` CLI session if one exists; otherwise this will "
            "fail to authenticate.",
            file=sys.stderr,
        )
    if args.deploy and not os.environ.get("VERCEL_TOKEN"):
        print(
            "[warn] --deploy requested but VERCEL_TOKEN is not set. The "
            "Deployer stage will fail at `vercel deploy`.",
            file=sys.stderr,
        )

    sys.exit(asyncio.run(_main_async(args)))


if __name__ == "__main__":
    main()

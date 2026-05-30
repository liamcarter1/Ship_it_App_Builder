"""Agent factories.

Each agent module exposes both an `AgentDefinition` factory (for subagent
reuse) and a `build_<role>_options(workspace, ...)` helper that returns a
`ClaudeAgentOptions` configured for running that role as the main agent of a
`query()` call. The Milestone 1 orchestrator uses the latter for each stage.
"""

from .planner import build_planner, build_planner_options, parse_planner_spec
from .coder import (
    build_coder,
    build_coder_options,
    build_coder_brief,
    build_coder_revision_brief,
)
from .scaffolder import build_scaffolder, build_scaffolder_options
from .reviewer import (
    build_reviewer,
    build_reviewer_options,
    parse_reviewer_verdict,
)
from .deployer import (
    build_deployer,
    build_deployer_options,
    parse_deployer_result,
)

__all__ = [
    "build_planner",
    "build_planner_options",
    "parse_planner_spec",
    "build_coder",
    "build_coder_options",
    "build_coder_brief",
    "build_coder_revision_brief",
    "build_scaffolder",
    "build_scaffolder_options",
    "build_reviewer",
    "build_reviewer_options",
    "parse_reviewer_verdict",
    "build_deployer",
    "build_deployer_options",
    "parse_deployer_result",
]

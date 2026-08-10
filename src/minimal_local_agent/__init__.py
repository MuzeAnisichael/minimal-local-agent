"""A small, auditable local-first agent."""

from minimal_local_agent.runtime import AgentRuntime, RunOutcome, RuntimeEvent

__version__ = "0.5.0"

__all__ = [
    "AgentRuntime",
    "RunOutcome",
    "RuntimeEvent",
    "__version__",
]

"""A small, auditable local-first agent."""

from minimal_local_agent.read_tools import ReadTool
from minimal_local_agent.runtime import AgentRuntime, RunOutcome, RuntimeEvent

__version__ = "0.6.0"

__all__ = [
    "AgentRuntime",
    "ReadTool",
    "RunOutcome",
    "RuntimeEvent",
    "__version__",
]

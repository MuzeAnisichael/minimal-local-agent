"""A small, auditable local-first agent."""

from minimal_local_agent.agent import AgentDependencies
from minimal_local_agent.config import MCPServerSettings, Settings
from minimal_local_agent.context import ContextBudgetExceeded, ContextReducer
from minimal_local_agent.memory import (
    MemoryConflictError,
    MemoryRecord,
    MemoryStore,
    SQLiteMemoryStore,
)
from minimal_local_agent.models import ModelFactory
from minimal_local_agent.peers import LocalPeer, Peer, PeerResult
from minimal_local_agent.planning import ModelPlanner, Plan, Planner, PlanStep
from minimal_local_agent.read_tools import ReadTool
from minimal_local_agent.runtime import (
    AgentRuntime,
    EventHandler,
    RunOutcome,
    RuntimeEvent,
)

__version__ = "1.0.0"

__all__ = [
    "AgentRuntime",
    "AgentDependencies",
    "ContextBudgetExceeded",
    "ContextReducer",
    "EventHandler",
    "MCPServerSettings",
    "ModelFactory",
    "MemoryConflictError",
    "MemoryRecord",
    "MemoryStore",
    "SQLiteMemoryStore",
    "ModelPlanner",
    "Plan",
    "Planner",
    "PlanStep",
    "LocalPeer",
    "Peer",
    "PeerResult",
    "ReadTool",
    "RunOutcome",
    "RuntimeEvent",
    "Settings",
    "__version__",
]

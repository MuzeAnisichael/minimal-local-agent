"""Synchronous embedding with a scoped SDK model/toolset lifecycle."""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic_ai import Agent


def managed_run(agent: Agent[Any, Any], prompt: str, **kwargs: Any) -> Any:
    # Same synchronous boundary as run_sync; async hosts should use to_thread.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("Use asyncio.to_thread for the synchronous runtime API")

    async def execute() -> Any:
        async with agent:
            return await agent.run(prompt, **kwargs)

    return asyncio.run(execute())

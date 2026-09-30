"""Run one trusted Python read tool without changing the agent core."""

from dataclasses import replace

from pydantic_ai import RunContext

from minimal_local_agent import AgentDependencies, AgentRuntime, ReadTool, Settings


def count_files(ctx: RunContext[AgentDependencies]) -> dict[str, int]:
    """Count files visible inside the configured workspace."""
    return {"count": len(ctx.deps.workspace.list_files("."))}


if __name__ == "__main__":
    settings = replace(Settings.load(), write_policy="deny")
    runtime = AgentRuntime(
        settings,
        read_tools=(ReadTool("count_files", count_files),),
    )
    print(runtime.run("调用 count_files，告诉我工作区有多少文件。").response)

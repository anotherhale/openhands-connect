"""MCP server assembly.

Builds the tool surface over a ``ConnectService``. The service is passed in so
tests can drive the real handlers with a fake provider and no network.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from openhands_connect.mcp_server.tools import (
    EnvironmentIdArg,
    LimitArg,
    MCPTools,
    MessageArg,
    PromptArg,
    TaskIdArg,
)
from openhands_connect.service import ConnectService

SERVER_NAME = "openhands-connect"
SERVER_VERSION = "0.1.0"

_EXECUTION_WARNING = (
    "Task execution runs an AI agent inside the selected environment: it may read "
    "and modify files and run shell commands there. Only target environments you "
    "trust with that access."
)


def build_server(service: ConnectService) -> MCPServer:
    tools = MCPTools(service)
    server: MCPServer = MCPServer(
        name=SERVER_NAME,
        version=SERVER_VERSION,
        instructions=(
            "Bridge to explicitly configured OpenHands execution environments. "
            "Call list_environments first, then pass an environment_id to every "
            "other tool. There is no default environment and no local/cloud "
            "fallback. " + _EXECUTION_WARNING
        ),
    )

    @server.tool(
        name="list_environments",
        description=(
            "List the configured OpenHands environments. Returns sanitized "
            "metadata only (id, name, provider, base URL, enabled flag, and the "
            "operations each environment permits). Read-only; contacts no server."
        ),
    )
    async def list_environments() -> dict[str, Any]:
        return await tools.list_environments()

    @server.tool(
        name="check_environment",
        description=(
            "Check connectivity to ONE explicitly named environment and report "
            "whether it is reachable and which server version answered. Read-only: "
            "sends no task and changes nothing."
        ),
    )
    async def check_environment(environment_id: EnvironmentIdArg) -> dict[str, Any]:
        return await tools.check_environment(environment_id)

    @server.tool(
        name="create_task",
        description=(
            "Submit a task to ONE explicitly named environment and return its task "
            "ID. "
            + _EXECUTION_WARNING
            + " Fails if the environment's policy does not permit task creation."
        ),
    )
    async def create_task(
        environment_id: EnvironmentIdArg,
        prompt: PromptArg,
        workspace: str | None = None,
        max_iterations: int | None = None,
        agent_profile_id: str | None = None,
    ) -> dict[str, Any]:
        return await tools.create_task(
            environment_id,
            prompt,
            workspace=workspace,
            max_iterations=max_iterations,
            agent_profile_id=agent_profile_id,
        )

    @server.tool(
        name="get_task",
        description=(
            "Get the normalized lifecycle state of a task in ONE named environment. "
            "The response includes the raw upstream status alongside the normalized "
            "state; state is 'unknown' when upstream's status is unrecognized or "
            "missing rather than being guessed."
        ),
    )
    async def get_task(environment_id: EnvironmentIdArg, task_id: TaskIdArg) -> dict[str, Any]:
        return await tools.get_task(environment_id, task_id)

    @server.tool(
        name="get_task_logs",
        description=(
            "Retrieve a bounded, redacted slice of a task's event log from ONE named "
            "environment. Returns at most 'limit' entries (default 50, max 200); each "
            "entry's text is truncated and credential-like strings are redacted."
        ),
    )
    async def get_task_logs(
        environment_id: EnvironmentIdArg, task_id: TaskIdArg, limit: LimitArg = 50
    ) -> dict[str, Any]:
        return await tools.get_task_logs(environment_id, task_id, limit=limit)

    @server.tool(
        name="send_task_message",
        description=(
            "Send a follow-up instruction to an existing task in ONE named "
            "environment. This continues the same task; it does not start a new "
            "one. The message may cause further file changes or commands in that "
            "environment."
        ),
    )
    async def send_task_message(
        environment_id: EnvironmentIdArg, task_id: TaskIdArg, message: MessageArg
    ) -> dict[str, Any]:
        return await tools.send_task_message(environment_id, task_id, message)

    @server.tool(
        name="stop_task",
        description=(
            "Request that a task stop. LIMITATION: OpenHands Agent Server has no "
            "endpoint that terminates a run, so this interrupts the task and leaves "
            "it paused. The conversation stays resumable: send_task_message on the "
            "paused task continues it. The response reports cancelled=false and the "
            "resulting state rather than claiming the task was cancelled."
        ),
    )
    async def stop_task(environment_id: EnvironmentIdArg, task_id: TaskIdArg) -> dict[str, Any]:
        return await tools.stop_task(environment_id, task_id)

    @server.tool(
        name="get_task_result",
        description=(
            "Return the agent's final response for a COMPLETED task in ONE named "
            "environment. When the task has not completed, or the server has no "
            "final response, available=false with a reason is returned instead of "
            "partial or fabricated output."
        ),
    )
    async def get_task_result(
        environment_id: EnvironmentIdArg, task_id: TaskIdArg
    ) -> dict[str, Any]:
        return await tools.get_task_result(environment_id, task_id)

    return server


__all__ = ["SERVER_NAME", "SERVER_VERSION", "build_server"]

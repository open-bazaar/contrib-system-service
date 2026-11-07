"""Manage the user-level host service that keeps the node running."""

from __future__ import annotations

import asyncio
import re
import signal
from contextlib import ExitStack, asynccontextmanager

from bazaar_contrib_config import CONFIGURATION
from bazaar_contrib_invoke import COMMANDS, CommandEntry
from bazaar_contrib_sidecar import CLIENT, RemoteEntry
from bazaar_contrib_status import CONTRIB_STATUS, ContribStatusQuery
from bazaar_contrib_timer import TIMER
from bazaar_core import Context, Contrib, ServiceKey, Starting

from .cli import DEPRECATED, DESCRIPTION, create_commands
from .service import SystemService

__all__ = [
    "PROCESS",
    "READINESS",
    "Process",
    "Readiness",
    "command_entry",
    "commands",
    "process",
    "process_remote",
    "readiness",
    "readiness_remote",
    "restart_entry",
    "start_entry",
    "stop_entry",
]


class Readiness:
    """Whether every configured contrib of this node has finished starting."""

    def __init__(self, status: ContribStatusQuery) -> None:
        self._status = status

    def ready(self) -> bool:
        return not any(
            isinstance(record.state, Starting)
            for record in self._status.snapshot()
            if re.fullmatch(r"contribs\[\d+\]\.enabled\[\d+\]", record.location)
        )


READINESS = ServiceKey[Readiness]("system-service.readiness")


class Process:
    """The node process this service runs."""

    def restart(self) -> None:
        """End the node as Ctrl+C does, for the supervisor to start it again."""
        signal.raise_signal(signal.SIGINT)


PROCESS = ServiceKey[Process]("system-service.process")


@asynccontextmanager
async def _readiness(ctx: Context):
    ctx.offer(READINESS, Readiness(ctx.accept(CONTRIB_STATUS)))
    yield


@asynccontextmanager
async def _process(ctx: Context):
    ctx.offer(PROCESS, Process())
    yield


@asynccontextmanager
async def _commands(ctx: Context):
    service = SystemService(
        ctx.accept(CONFIGURATION), ctx.accept(CLIENT), ctx.accept(TIMER)
    )
    registry = ctx.accept(COMMANDS)
    with ExitStack() as stack:
        for command in await asyncio.to_thread(create_commands, service):
            stack.enter_context(registry.register(command))
        yield


readiness = Contrib(accepts=[CONTRIB_STATUS], offers=[READINESS], lifespan=_readiness)
readiness_remote = RemoteEntry(READINESS, ("ready",))
process = Contrib(offers=[PROCESS], lifespan=_process)
process_remote = RemoteEntry(PROCESS, ("restart",))
commands = Contrib(accepts=[CONFIGURATION, COMMANDS, CLIENT, TIMER], lifespan=_commands)
command_entry = CommandEntry(contrib="system-service", help=DESCRIPTION)
start_entry = CommandEntry(contrib="system-service", help=DEPRECATED["start"])
stop_entry = CommandEntry(contrib="system-service", help=DEPRECATED["stop"])
restart_entry = CommandEntry(contrib="system-service", help=DEPRECATED["restart"])

"""Call the node in BCN_HOME through its sidecar, as bcc does."""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager

from bazaar_contrib_sidecar import CLIENT, Client
from bazaar_core import Context, Contrib, ContribEntry, ContribHub, ContribSpec, load


async def call(service: str, method: str) -> object:
    clients: list[Client] = []

    @asynccontextmanager
    async def caller(ctx: Context):
        clients.append(ctx.accept(CLIENT))
        yield

    composition = await asyncio.to_thread(
        load,
        [
            ContribSpec(contrib="timer-wheel"),
            ContribSpec(
                contrib="sidecar-windows" if sys.platform == "win32" else "sidecar-unix"
            ),
        ],
    )
    composition = composition.model_copy(
        update={
            "entries": (
                *composition.entries,
                ContribEntry(
                    name="caller", definition=Contrib(accepts=[CLIENT], lifespan=caller)
                ),
            )
        }
    )
    async with ContribHub() as hub, hub.enable(composition):
        return await clients[0].call(service, method)

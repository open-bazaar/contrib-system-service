"""The `bcn system-service` group and the deprecated names that forward to it."""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from .service import SystemService

DESCRIPTION = "Manage the user-level host service for bcn."
HELP = {
    "install": "Register bcn to start on the next user login.",
    "start": "Start the registered host service through the native service manager.",
    "stop": "Stop the registered host service.",
    "restart": "Restart the registered host service.",
    "uninstall": "Remove the registered host service and its managed files.",
    "status": "Report host service registration and bcn health.",
}
DEPRECATED = {
    name: f"Deprecated; forwards to `bcn system-service {name}`."
    for name in ("start", "stop", "restart")
}


def create_commands(service: SystemService) -> tuple[click.Command, ...]:
    @click.group("system-service", help=DESCRIPTION)
    def group() -> None: ...

    @group.command("install", help=HELP["install"], short_help=HELP["install"])
    @click.option(
        "--env-file",
        type=click.Path(path_type=Path),
        help=(
            "Platform-compatible environment file; on Windows this is a "
            "PowerShell environment script."
        ),
    )
    @click.option(
        "--force",
        is_flag=True,
        help=(
            "Overwrite service files, and on Windows the scheduled task, that "
            "bcn did not create."
        ),
    )
    def install(env_file: Path | None, force: bool):
        return service.run("install", env_file=env_file, force=force)

    for name in ("start", "stop", "restart", "uninstall", "status"):

        @group.command(name, help=HELP[name], short_help=HELP[name])
        def command(_name: str = name):
            return service.run(_name)

    commands: list[click.Command] = [group]
    for name, help_text in DEPRECATED.items():

        @click.command(name, help=help_text, short_help=help_text)
        def deprecated(_name: str = name):
            # these names now belong to the service manager, and the node only
            # forwards them so an old habit still lands somewhere sensible
            async def forward() -> None:
                await asyncio.to_thread(
                    click.echo,
                    f"DeprecationWarning: `bcn {_name}` is deprecated; use "
                    f"`bcn system-service {_name}` for host-managed execution.",
                    err=True,
                )
                await service.run(_name)

            return forward()

        commands.append(deprecated)
    return tuple(commands)


__all__ = ["DEPRECATED", "DESCRIPTION", "create_commands"]

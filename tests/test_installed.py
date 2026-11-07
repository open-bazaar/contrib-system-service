from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from remote import call

RUNNER = """import sys

from bazaar_compute_node.cli import main
from blockbuster import blockbuster_ctx

with blockbuster_ctx():
    main.main(args=sys.argv[1:], prog_name="bcn")
"""

PLUGIN = """
[[contribs]]
package = "bazaar-contrib-system-service @ {uri}"
[[contribs.enabled]]
entry = "system-service"
[[contribs.enabled]]
entry = "system-service-process"
[[contribs.enabled]]
entry = "system-service-readiness"
"""


@pytest.fixture(scope="module")
def distributions(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("distributions")
    subprocess.run(
        [
            "uv",
            "build",
            str(Path(__file__).resolve().parents[1]),
            "--no-sources",
            "--out-dir",
            str(output),
        ],
        cwd=output,
        check=True,
    )
    return output


@dataclass(frozen=True)
class Installed:
    """bcn with one distribution of this package, its home in a directory of its own."""

    artifact: Path
    home: Path

    async def start(self, *arguments: str) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            "uv",
            "run",
            "--isolated",
            "--no-project",
            "--python",
            "3.15",
            "--prerelease",
            "allow",
            "--with",
            "bazaar-compute-node>=0.3.0rc13,<0.4",
            "--with",
            str(self.artifact),
            "--with",
            "blockbuster>=1.5.29,<1.6",
            "python",
            "-I",
            str(self.home / "bcn.py"),
            "--configer-options",
            str(self.home / ".bcn" / "config.toml"),
            *arguments,
            cwd=self.home,
            env={
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("BCN_", "BCC_"))
            }
            | {"BCN_HOME": str(self.home)},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

    async def run(self, *arguments: str) -> str:
        process = await self.start(*arguments)
        text = (await process.communicate())[0].decode()
        assert process.returncode == 0, text
        return text


async def installed(distributions: Path, home: Path, suffix: str) -> Installed:
    """bcn bootstrapped in the home, with every contrib of the distribution enabled."""
    artifact = await asyncio.to_thread(lambda: next(distributions.glob(suffix)))
    await asyncio.to_thread((home / "bcn.py").write_text, RUNNER)
    node = Installed(artifact, home)
    bootstrap = await node.run("--help")
    config = home / ".bcn" / "config.toml"
    assert await asyncio.to_thread(config.exists), bootstrap
    document = await asyncio.to_thread(config.read_text)
    await asyncio.to_thread(
        config.write_text, document + PLUGIN.format(uri=artifact.as_uri())
    )
    return node


@pytest.mark.parametrize("suffix", ["*.whl", "*.tar.gz"], ids=["wheel", "sdist"])
async def test_installed_distribution_adds_the_service_commands(
    distributions: Path, tmp_path: Path, suffix: str
):
    node = await installed(distributions, tmp_path, suffix)
    assert "system-service" in await node.run("--help")
    commands = await node.run("system-service", "--help")
    for command in ("install", "start", "stop", "restart", "status", "uninstall"):
        assert command in commands
    install = await node.run("system-service", "install", "--help")
    assert "--env-file" in install and "--force" in install
    assert "bcn system-service start" in await node.run("start", "--help")


async def test_a_restart_through_the_sidecar_ends_the_node_as_ctrl_c_does(
    distributions: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    node = await installed(distributions, tmp_path, "*.whl")
    process = await node.start("run")
    try:
        assert process.stdout is not None
        output = b""
        while b"bcn ready" not in output:
            line = await process.stdout.readline()
            assert line, output.decode()
            output += line
        monkeypatch.setenv("BCN_HOME", str(tmp_path))
        await call("system-service", "restart")
        await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("suffix", ["*.whl", "*.tar.gz"], ids=["wheel", "sdist"])
async def test_installed_distribution_adds_the_service_commands(
    distributions: Path, tmp_path: Path, suffix: str
):
    artifact = await asyncio.to_thread(lambda: next(distributions.glob(suffix)))
    runner = tmp_path / "bcn.py"
    await asyncio.to_thread(runner.write_text, RUNNER)
    config = tmp_path / ".bcn" / "config.toml"
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("BCN_", "BCC_"))
    } | {"BCN_HOME": str(tmp_path)}

    async def bcn(*arguments: str) -> str:
        process = await asyncio.create_subprocess_exec(
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
            str(artifact),
            "--with",
            "blockbuster>=1.5.29,<1.6",
            "python",
            "-I",
            str(runner),
            "--configer-options",
            str(config),
            *arguments,
            cwd=tmp_path,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        text = (await process.communicate())[0].decode()
        assert process.returncode == 0, text
        return text

    bootstrap = await bcn("--help")
    assert await asyncio.to_thread(config.exists), bootstrap
    document = await asyncio.to_thread(config.read_text)
    await asyncio.to_thread(
        config.write_text, document + PLUGIN.format(uri=artifact.as_uri())
    )
    assert "system-service" in await bcn("--help")
    commands = await bcn("system-service", "--help")
    for command in ("install", "start", "stop", "restart", "status", "uninstall"):
        assert command in commands
    install = await bcn("system-service", "install", "--help")
    assert "--env-file" in install and "--force" in install
    assert "bcn system-service start" in await bcn("start", "--help")

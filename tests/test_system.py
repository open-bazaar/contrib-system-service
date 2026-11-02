"""Drive the native service manager through the installed commands on an isolated runner."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from bazaar_contrib_system_service.service import (
    LAUNCHD_LABEL,
    SYSTEMD_UNIT_NAME,
)

pytestmark = pytest.mark.system

PLUGIN = """
[[contribs]]
package = "bazaar-contrib-system-service @ {uri}"
[[contribs.enabled]]
entry = "system-service"
[[contribs.enabled]]
entry = "system-service-readiness"
"""


@dataclass(frozen=True)
class Node:
    root: Path
    bcn: Path
    environment: dict[str, str]
    data_dir: Path

    def run(self, *arguments: str) -> str:
        result = subprocess.run(
            [str(self.bcn), *arguments],
            check=False,
            env=self.environment,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        assert result.returncode == 0, result.stdout + result.stderr + self.logs()
        return result.stdout

    def logs(self) -> str:
        """The package operations recorded so far, then the service's output."""
        parts = [
            path.read_text(encoding="utf-8", errors="replace")
            for path in sorted(
                self.data_dir.glob("contrib/environments/*/operations/*.json")
            )
        ]
        if sys.platform == "linux":
            journal = subprocess.run(
                ["journalctl", "--user", "--unit", SYSTEMD_UNIT_NAME, "--no-pager"],
                check=False,
                env=self.environment,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
            )
            parts.append(journal.stdout + journal.stderr)
        elif (log := self.data_dir / "system-service.log").exists():
            parts.append(log.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(parts)

    def managed_files(self) -> list[Path]:
        paths = [self.data_dir / "supervisor.py"]
        if sys.platform == "linux":
            config_home = os.environ.get("XDG_CONFIG_HOME")
            config = Path(config_home) if config_home else Path.home() / ".config"
            paths.append(config / "systemd" / "user" / SYSTEMD_UNIT_NAME)
        elif sys.platform == "darwin":
            agents = Path.home() / "Library" / "LaunchAgents"
            paths += [agents / f"{LAUNCHD_LABEL}.plist", agents / "bcn-run.sh"]
        else:
            paths += [
                self.data_dir / f"bcn-system-service.{suffix}"
                for suffix in ("xml", "ps1", "vbs")
            ]
        return paths

    def node_process(self) -> int | None:
        """The process running the service's copy of bcn, child of the supervisor."""
        # Match on the ASCII root and the copy's name, however a tool prints the
        # non-ASCII part of the path between them.
        name = "bcn.running.exe" if os.name == "nt" else "bcn.running"
        if os.name == "nt":
            script = "\n".join(
                (
                    f"$root = '{str(self.root).lower()}'",
                    f"$name = '{name}'",
                    "Get-CimInstance Win32_Process | Where-Object {",
                    "    $_.ProcessId -ne $PID -and $_.CommandLine -and",
                    "    $_.CommandLine.ToLowerInvariant().Contains($root) -and",
                    "    $_.CommandLine.ToLowerInvariant().Contains($name)",
                    '} | ForEach-Object { "$($_.ProcessId) $($_.ParentProcessId)" }',
                )
            )
            command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
            output = subprocess.run(
                [*command, script],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=True,
            ).stdout
            rows = [line.split() for line in output.splitlines() if line.strip()]
        else:
            output = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,args="],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=True,
            ).stdout
            rows = [
                fields[:2]
                for fields in (line.split(maxsplit=2) for line in output.splitlines())
                if len(fields) == 3
                and str(self.root) in fields[2]
                and name in fields[2]
            ]
        processes = {int(pid): int(parent) for pid, parent in rows}
        tops = [pid for pid, parent in processes.items() if parent not in processes]
        return tops[0] if len(tops) == 1 else None


def eventually(check: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            return False
        time.sleep(1)
    return True


@pytest.fixture
def node(tmp_path: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Node]:
    if os.environ.get("BCN_SYSTEM_SERVICE_RUNNER") != "isolated":
        pytest.fail(
            "system tests drive this machine's service manager; run them on an "
            "isolated runner that sets BCN_SYSTEM_SERVICE_RUNNER=isolated"
        )
    # The manager starts the Python of environments it stages deep under BCN_HOME,
    # which must stay within Windows' MAX_PATH, so the home sits on the basetemp.
    home = tmp_path_factory.mktemp("${T} %Q '夜班'")
    binaries = tmp_path / "bin ${T} %Q"
    bcn = binaries / ("bcn.exe" if os.name == "nt" else "bcn")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("BCN_", "BCC_"))
    } | {
        "BCN_HOME": str(home),
        "UV_TOOL_DIR": str(tmp_path / "tools"),
        "UV_TOOL_BIN_DIR": str(binaries),
        "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
    }
    try:
        subprocess.run(
            [
                "uv",
                "build",
                str(Path(__file__).resolve().parents[1]),
                "--wheel",
                "--no-sources",
                "--out-dir",
                str(tmp_path / "dist"),
            ],
            cwd=tmp_path,
            check=True,
        )
        wheel = next((tmp_path / "dist").glob("*.whl"))
        subprocess.run(
            [
                "uv",
                "tool",
                "install",
                "--python",
                "3.15",
                "--prerelease",
                "allow",
                "bazaar-compute-node>=0.3.0rc13,<0.4",
            ],
            cwd=tmp_path,
            env=environment,
            check=True,
        )
        node = Node(home.parent, bcn, environment, home / ".bcn")
        bootstrap = node.run("--help")
        config = node.data_dir / "config.toml"
        assert config.is_file(), bootstrap + node.logs()
        config.write_text(
            config.read_text(encoding="utf-8") + PLUGIN.format(uri=wheel.as_uri()),
            encoding="utf-8",
        )
        assert "system-service" in node.run("--help")
        yield node
    finally:
        if bcn.exists():
            for command in ("stop", "uninstall"):
                subprocess.run(
                    [str(bcn), "system-service", command],
                    check=False,
                    env=environment,
                    capture_output=True,
                )


def test_installed_commands_drive_the_native_service(node: Node):
    assert "system service installed" in node.run("system-service", "install")
    assert all(path.is_file() for path in node.managed_files())

    assert "health=ready" in node.run("system-service", "start")
    status = node.run("system-service", "status")
    for state in ("installed=yes", "enabled=yes", "active=yes", "health=ready"):
        assert state in status, status

    first = node.node_process()
    assert first is not None
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(first), "/T", "/F"],
            capture_output=True,
            check=True,
        )
    else:
        os.kill(first, signal.SIGKILL)
    assert eventually(lambda: node.node_process() not in (None, first), 120), (
        node.logs()
    )
    assert eventually(
        lambda: "health=ready" in node.run("system-service", "status"), 300
    ), node.logs()

    assert "health=ready" in node.run("system-service", "restart")

    node.run("system-service", "stop")
    status = node.run("system-service", "status")
    assert "active=no" in status and "health=unreachable" in status, status

    node.run("system-service", "uninstall")
    assert not any(path.exists() for path in node.managed_files())
    assert "installed=no" in node.run("system-service", "status")

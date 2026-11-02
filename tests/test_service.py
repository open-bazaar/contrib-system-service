from __future__ import annotations

import codecs
import json
import os
import plistlib
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from xml.etree import ElementTree

import pytest

from bazaar_contrib_system_service.service import (
    MANAGED_MARKER,
    SystemServiceContext,
    remove_managed_file,
    render_launchd_plist,
    render_systemd_unit,
    render_windows_launcher,
    render_windows_task,
    render_windows_wrapper,
    write_managed_file,
)


def context(root: Path) -> SystemServiceContext:
    data_dir = root / "installed ${TEAM} %Q '夜班'" / ".bcn"
    return SystemServiceContext(
        executable=root / "tools bin" / "bcn",
        python=Path(sys.executable),
        node_options=(
            "--configer",
            "config-filesystem",
            "--configer-options",
            str(data_dir / "config ${TEAM} %h.toml"),
        ),
        data_dir=data_dir,
        env_file=root / ".config" / "bcn" / "runtime.env",
        log_path=data_dir / "system-service.log",
        user="editor",
    )


def test_systemd_unit_starts_the_supervisor_with_the_configer_selection(
    tmp_path: Path,
):
    service = context(tmp_path)
    unit = render_systemd_unit(service)
    values = dict(line.split("=", 1) for line in unit.splitlines() if "=" in line)
    command = shlex.split(values["ExecStart"].removeprefix(":env ").replace("%%", "%"))
    assert command[0] == f"BCN_HOME={service.data_dir.parent}"
    assert command[2] == str(service.data_dir / "supervisor.py")
    assert command[command.index("--executable") + 1] == str(service.executable)
    assert tuple(command[-4:]) == service.node_options
    assert values["WorkingDirectory"].replace("%%", "%") == str(service.data_dir)
    assert values["EnvironmentFile"].replace("%%", "%") == f"-{service.env_file}"
    assert MANAGED_MARKER in unit


def test_launchd_agent_passes_the_configer_selection_to_its_wrapper(tmp_path: Path):
    service = context(tmp_path)
    wrapper = tmp_path / "LaunchAgents" / "bcn-run.sh"
    plist = plistlib.loads(render_launchd_plist(service, wrapper))
    assert plist["ProgramArguments"][0] == str(wrapper)
    assert tuple(plist["ProgramArguments"][1:]) == service.node_options
    assert plist["EnvironmentVariables"]["BCN_HOME"] == str(service.data_dir.parent)
    assert plist["WorkingDirectory"] == str(service.data_dir)
    assert plist["Comment"] == MANAGED_MARKER


def test_windows_task_runs_the_wrapper_through_its_launcher(tmp_path: Path):
    service = context(tmp_path)
    assert render_windows_wrapper(service).startswith(codecs.BOM_UTF8)
    launcher_path = service.data_dir / "bcn-system-service.vbs"
    launcher = render_windows_launcher(service.data_dir / "bcn-system-service.ps1")
    assert launcher.startswith(codecs.BOM_UTF16)
    assert str(service.data_dir / "bcn-system-service.ps1") in launcher.decode("utf-16")
    task = ElementTree.fromstring(
        render_windows_task(service, launcher_path).decode("utf-16").split("?>", 1)[1]
    )
    namespace = {"task": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert str(launcher_path) in task.findtext(
        "task:Actions/task:Exec/task:Arguments", default="", namespaces=namespace
    )
    assert task.findtext(
        "task:Actions/task:Exec/task:WorkingDirectory", namespaces=namespace
    ) == str(service.data_dir)


@pytest.mark.skipif(os.name != "nt", reason="Windows parses its own command lines")
def test_windows_wrapper_passes_the_supervisor_its_arguments_unchanged(
    tmp_path: Path,
):
    service = replace(
        context(tmp_path),
        node_options=(
            "--configer",
            "inline",
            "--configer-options",
            '{"token": "two words"}',
            "C:\\data dir\\",
        ),
    )
    wrapper = render_windows_wrapper(service).decode("utf-8-sig")
    literal = re.search(r"^\$arguments = '((?:[^']|'')*)'$", wrapper, re.MULTILINE)
    assert literal is not None
    echo = "import json, sys; print(json.dumps(sys.argv[1:]))"
    result = subprocess.run(
        f'"{sys.executable}" -c "{echo}" ' + literal.group(1).replace("''", "'"),
        capture_output=True,
        check=True,
        text=True,
    )
    assert json.loads(result.stdout) == [
        str(service.data_dir / "supervisor.py"),
        "--executable",
        str(service.executable),
        *service.node_options,
    ]


def test_managed_files_replace_their_own_and_refuse_others(tmp_path: Path):
    unit = tmp_path / "systemd" / "bcn.service"
    write_managed_file(unit, f"# {MANAGED_MARKER}\n[Unit]\n", mode=0o600)
    write_managed_file(unit, f"# {MANAGED_MARKER}\n[Service]\n", mode=0o600)
    assert "[Service]" in unit.read_text()
    if os.name != "nt":
        assert unit.stat().st_mode & 0o777 == 0o600

    handwritten = tmp_path / "systemd" / "handwritten.service"
    handwritten.write_text("[Unit]\nDescription=Written by hand\n")
    with pytest.raises(RuntimeError, match="refusing to overwrite unmanaged file"):
        write_managed_file(handwritten, f"# {MANAGED_MARKER}\n", mode=0o600)
    with pytest.raises(RuntimeError, match="refusing to remove unmanaged file"):
        remove_managed_file(handwritten)
    write_managed_file(handwritten, f"# {MANAGED_MARKER}\n", mode=0o600, force=True)
    assert MANAGED_MARKER in handwritten.read_text()


@pytest.mark.skipif(os.name == "nt", reason="the Windows child is an executable image")
def test_supervisor_restarts_a_fresh_copy_of_a_read_only_installed_bcn(
    tmp_path: Path,
):
    data_dir = tmp_path / ".bcn"
    data_dir.mkdir()
    supervisor = data_dir / "supervisor.py"
    supervisor.write_bytes(
        files("bazaar_contrib_system_service")
        .joinpath("resources/supervisor.py")
        .read_bytes()
    )
    record = tmp_path / "runs.log"
    executable = tmp_path / "bin" / "bcn"
    executable.parent.mkdir()

    def install(release: str) -> None:
        executable.unlink(missing_ok=True)
        executable.write_text(
            f'#!/bin/sh\necho "{release} $*" >> "{record}"\nsleep 0.2\n'
        )
        executable.chmod(0o555)

    install("0.3.0rc1")
    process = subprocess.Popen(
        [
            sys.executable,
            str(supervisor),
            "--executable",
            str(executable),
            "--configer",
            "config-filesystem",
            "--configer-options",
            str(tmp_path / "config.toml"),
        ],
        stdout=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while "0.3.0rc1" not in (record.read_text() if record.exists() else ""):
            assert time.monotonic() < deadline
            time.sleep(0.1)
        install("0.3.0rc2")
        while "0.3.0rc2" not in record.read_text():
            assert time.monotonic() < deadline
            time.sleep(0.1)
    finally:
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 0
    runs = record.read_text()
    assert (
        f"run --configer config-filesystem --configer-options {tmp_path / 'config.toml'}"
        in runs
    )
    assert (data_dir / "bcn.running").read_text().startswith("#!/bin/sh")

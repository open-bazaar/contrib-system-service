"""Register, drive and inspect the user-level host service that runs the node."""

from __future__ import annotations

import asyncio
import getpass
import locale
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import click
from bazaar_contrib_config import Configuration
from bazaar_contrib_sidecar import Client, RemoteError
from bazaar_contrib_timer import Timer
from jinja2 import Environment, PackageLoader, StrictUndefined

SYSTEMD_UNIT_NAME = "bcn.service"
LAUNCHD_LABEL = "io.github.yuchanns.bazaar-compute-node"
WINDOWS_TASK_NAME = r"\BazaarComputeNode"
MANAGED_MARKER = "Managed by bazaar-compute-node."
WINDOWS_STOP_ATTEMPTS = 100
WINDOWS_STOP_INTERVAL = 0.05
READY_WAIT_MS = 300_000
NOT_INSTALLED = (
    "system service is not installed; run `bcn system-service install` first"
)

_TEMPLATES = Environment(
    loader=PackageLoader("bazaar_contrib_system_service", "resources"),
    undefined=StrictUndefined,
    autoescape=False,
    keep_trailing_newline=True,
)


@dataclass(frozen=True, slots=True)
class SystemServiceContext:
    """Resolved paths used to render one user-level system service."""

    executable: Path | None
    python: Path
    node_options: tuple[str, ...]
    data_dir: Path
    env_file: Path
    log_path: Path
    user: str


@dataclass(frozen=True, slots=True)
class NativeServiceStatus:
    installed: bool
    enabled: bool | None
    active: bool | None
    detail: str


def resolve_data_dir() -> Path:
    path = Path(os.environ.get("BCN_HOME") or Path.home())
    return path.expanduser().resolve() / ".bcn"


def _resolve_executable() -> Path:
    executable = shutil.which("bcn")
    if executable is None:
        candidate = Path(sys.argv[0])
        if candidate.is_file():
            executable = str(candidate)
    if executable is None:
        raise RuntimeError(
            "cannot resolve the bcn executable; run this command through the installed bcn CLI"
        )
    return Path(executable).expanduser().absolute()


def _build_context(
    node_options: tuple[str, ...],
    env_file: Path | None,
    *,
    require_executable: bool,
) -> SystemServiceContext:
    executable = _resolve_executable() if require_executable else None
    data_dir = resolve_service_data_dir(registered=not require_executable)
    # the service reads this file unless told another; none of the platforms
    # minds the file not existing yet
    env_file = (env_file or default_env_file()).expanduser().resolve()
    return SystemServiceContext(
        executable=executable,
        python=Path(vars(sys)["_base_executable"]).resolve(),
        node_options=node_options,
        data_dir=data_dir,
        env_file=env_file,
        log_path=data_dir / "system-service.log",
        user=_resolve_current_user(),
    )


def resolve_service_data_dir(*, registered: bool = True) -> Path:
    """Read the data directory selected by an installed service, without account lookup."""

    data_dir = resolve_data_dir()
    if registered:
        system = platform.system()
        if system == "Linux":
            unit_path = _systemd_unit_path()
            if _is_managed(unit_path):
                values = dict(
                    line.split("=", 1)
                    for line in unit_path.read_text(encoding="utf-8").splitlines()
                    if "=" in line
                )
                data_dir = Path(values["WorkingDirectory"].replace("%%", "%"))
        elif system == "Darwin":
            plist_path, _ = _launchd_paths()
            if _is_managed(plist_path):
                with plist_path.open("rb") as stream:
                    values = plistlib.load(stream)
                data_dir = Path(values["WorkingDirectory"])
        elif system == "Windows":
            directory = _windows_service_data_dir()
            if directory is not None:
                data_dir = directory
    return data_dir


def _resolve_current_user() -> str:
    try:
        return getpass.getuser()
    except OSError:
        if platform.system() != "Windows":
            raise RuntimeError("cannot resolve the current user") from None
        result = _run_native_command(["whoami"], check=False)
        user = result.stdout.strip()
        if result.returncode == 0 and user:
            return user
        raise RuntimeError("cannot resolve the current Windows user")


def _require_executable(context: SystemServiceContext) -> Path:
    if context.executable is None:
        raise RuntimeError("the bcn executable is required to render a service")
    return context.executable


def _systemd_quote(path: Path | str, *, quote: bool = True) -> str:
    value = str(path).replace("%", "%%")
    return (
        '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"' if quote else value
    )


def _powershell_literal(path: Path | str | None) -> str:
    value = "" if path is None else str(path)
    return "'" + value.replace("'", "''") + "'"


def render_systemd_unit(context: SystemServiceContext) -> str:
    return _TEMPLATES.get_template("systemd.service").render(
        managed_marker=MANAGED_MARKER,
        executable=_systemd_quote(_require_executable(context)),
        python=_systemd_quote(context.python),
        supervisor_path=_systemd_quote(context.data_dir / "supervisor.py"),
        node_options=" ".join(
            _systemd_quote(option) for option in context.node_options
        ),
        data_dir=_systemd_quote(context.data_dir, quote=False),
        home_environment=_systemd_quote(f"BCN_HOME={context.data_dir.parent}"),
        environment_file=_systemd_quote(context.env_file, quote=False),
    )


def render_launchd_wrapper() -> str:
    return _TEMPLATES.get_template("launchd.sh").render(managed_marker=MANAGED_MARKER)


def render_launchd_plist(context: SystemServiceContext, wrapper_path: Path) -> bytes:
    return (
        _TEMPLATES.get_template("launchd.plist")
        .render(
            label=LAUNCHD_LABEL,
            managed_marker=MANAGED_MARKER,
            wrapper_path=str(wrapper_path),
            node_options=context.node_options,
            data_dir=str(context.data_dir),
            home_dir=str(context.data_dir.parent),
            environment_file=str(context.env_file),
            executable=str(_require_executable(context)),
            python=str(context.python),
            supervisor_path=str(context.data_dir / "supervisor.py"),
            log_path=str(context.log_path),
        )
        .encode("utf-8")
    )


# Windows PowerShell and wscript read a script without a BOM in the ANSI code
# page, which garbles any non-ASCII path in it.
def render_windows_wrapper(context: SystemServiceContext) -> bytes:
    arguments = subprocess.list2cmdline(
        (
            str(context.data_dir / "supervisor.py"),
            "--executable",
            str(_require_executable(context)),
            *context.node_options,
        )
    )
    return (
        _TEMPLATES.get_template("windows.ps1")
        .render(
            managed_marker=MANAGED_MARKER,
            python=_powershell_literal(context.python),
            arguments=_powershell_literal(arguments),
            home_dir=_powershell_literal(context.data_dir.parent),
            environment_script=_powershell_literal(context.env_file),
            log_path=_powershell_literal(context.log_path),
        )
        .encode("utf-8-sig")
    )


def _vbs_literal(value: Path | str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def render_windows_launcher(wrapper_path: Path) -> bytes:
    command = _vbs_literal(
        "powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden "
        f'-ExecutionPolicy Bypass -File "{wrapper_path}"'
    )
    return (
        _TEMPLATES.get_template("windows.vbs")
        .render(managed_marker=MANAGED_MARKER, command=command)
        .encode("utf-16")
    )


def render_windows_task(context: SystemServiceContext, launcher_path: Path) -> bytes:
    rendered = _TEMPLATES.get_template("windows.xml").render(
        user=context.user,
        managed_marker=MANAGED_MARKER,
        arguments=f'//B //Nologo "{launcher_path}"',
        data_dir=str(context.data_dir),
    )
    return rendered.encode("utf-16")


def write_managed_file(
    path: Path, content: str | bytes, *, mode: int, force: bool = False
) -> None:
    if path.exists():
        if not path.is_file():
            raise RuntimeError(f"managed service path is not a file: {path}")
        if not force and not _contains_managed_marker(path.read_bytes()):
            raise RuntimeError(
                f"refusing to overwrite unmanaged file: {path}; "
                "use --force to replace it"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as temporary_file:
            if isinstance(content, str):
                temporary_file.write(content.encode("utf-8"))
            else:
                temporary_file.write(content)
        if os.name != "nt":
            os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
        temporary_path = None
        if os.name != "nt":
            os.chmod(path, mode)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _require_managed(*paths: Path) -> None:
    for path in paths:
        if not path.exists():
            continue
        if not path.is_file():
            raise RuntimeError(f"managed service path is not a file: {path}")
        if not _contains_managed_marker(path.read_bytes()):
            raise RuntimeError(f"refusing to remove unmanaged file: {path}")


def remove_managed_file(path: Path) -> None:
    _require_managed(path)
    path.unlink(missing_ok=True)


def _is_managed(path: Path) -> bool:
    return path.is_file() and _contains_managed_marker(path.read_bytes())


def _contains_managed_marker(content: bytes) -> bool:
    if MANAGED_MARKER.encode("utf-8") in content:
        return True
    for encoding in ("utf-16", "utf-16-le", "utf-16-be"):
        try:
            if MANAGED_MARKER in content.decode(encoding):
                return True
        except UnicodeError:
            continue
    return False


def _run_native_command(
    command: list[str],
    *,
    check: bool = True,
    encoding: str | None = None,
) -> subprocess.CompletedProcess[str]:
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = subprocess.run(
        command,
        capture_output=True,
        check=False,
        text=True,
        encoding=encoding or locale.getencoding(),
        errors="replace",
        creationflags=creationflags,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(
            f"native service command failed ({result.returncode}): "
            f"{' '.join(command)}{suffix}"
        )
    return result


def _run_powershell(
    script: str, *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run a script whose output keeps paths outside the console code page intact."""
    return _run_native_command(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n" + script,
        ],
        check=check,
        encoding="utf-8",
    )


def _systemd_unit_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home) if config_home else Path.home() / ".config"
    return root / "systemd" / "user" / SYSTEMD_UNIT_NAME


def _launchd_paths() -> tuple[Path, Path]:
    directory = Path.home() / "Library" / "LaunchAgents"
    return (
        directory / f"{LAUNCHD_LABEL}.plist",
        directory / "bcn-run.sh",
    )


def _launchd_domain() -> str:
    assert sys.platform == "darwin"
    return f"gui/{os.getuid()}"


def _windows_paths(data_dir: Path) -> tuple[Path, Path, Path]:
    return (
        data_dir / "bcn-system-service.xml",
        data_dir / "bcn-system-service.ps1",
        data_dir / "bcn-system-service.vbs",
    )


def _install_linux(context: SystemServiceContext, *, force: bool) -> list[str]:
    _install_supervisor(context, force=force)
    unit_path = _systemd_unit_path()
    write_managed_file(unit_path, render_systemd_unit(context), mode=0o600, force=force)
    _run_native_command(["systemctl", "--user", "daemon-reload"])
    _run_native_command(["systemctl", "--user", "enable", SYSTEMD_UNIT_NAME])
    return [
        f"system service installed platform=linux path={unit_path}",
        f"Start with: systemctl --user start {SYSTEMD_UNIT_NAME}",
    ]


def _install_macos(context: SystemServiceContext, *, force: bool) -> list[str]:
    _install_supervisor(context, force=force)
    plist_path, wrapper_path = _launchd_paths()
    write_managed_file(wrapper_path, render_launchd_wrapper(), mode=0o700, force=force)
    write_managed_file(
        plist_path,
        render_launchd_plist(context, wrapper_path),
        mode=0o600,
        force=force,
    )
    return [
        f"system service installed platform=macos path={plist_path}",
        f"Start with: launchctl bootstrap gui/$(id -u) {plist_path}",
    ]


def _install_windows(context: SystemServiceContext, *, force: bool) -> list[str]:
    if not force and _windows_task_unmanaged():
        raise RuntimeError(
            f"refusing to overwrite unmanaged scheduled task: {WINDOWS_TASK_NAME}; "
            "use --force to replace it"
        )
    _install_supervisor(context, force=force)
    xml_path, wrapper_path, launcher_path = _windows_paths(context.data_dir)
    context.data_dir.mkdir(parents=True, exist_ok=True)
    write_managed_file(
        wrapper_path, render_windows_wrapper(context), mode=0o600, force=force
    )
    write_managed_file(
        launcher_path, render_windows_launcher(wrapper_path), mode=0o600, force=force
    )
    write_managed_file(
        xml_path, render_windows_task(context, launcher_path), mode=0o600, force=force
    )
    _run_native_command(
        ["schtasks", "/Create", "/TN", WINDOWS_TASK_NAME, "/XML", str(xml_path), "/F"]
    )
    return [
        f"system service installed platform=windows task={WINDOWS_TASK_NAME}",
        f"Start with: schtasks /Run /TN {WINDOWS_TASK_NAME}",
    ]


def _install_supervisor(context: SystemServiceContext, *, force: bool) -> None:
    write_managed_file(
        context.data_dir / "supervisor.py",
        files(__package__).joinpath("resources/supervisor.py").read_bytes(),
        mode=0o600,
        force=force,
    )


def default_env_file() -> Path:
    """The environment file a service reads unless told another, in the
    platform's format."""

    name = "runtime.ps1" if os.name == "nt" else "runtime.env"
    return Path.home() / ".config" / "bcn" / name


def _windows_task_unmanaged() -> bool:
    task_path = _powershell_literal("\\")
    task_name = _powershell_literal(WINDOWS_TASK_NAME.lstrip("\\"))
    marker = _powershell_literal(MANAGED_MARKER)
    query_script = "\n".join(
        (
            "$ErrorActionPreference = 'Stop'",
            (
                f"$task = Get-ScheduledTask -TaskPath {task_path} -TaskName {task_name}"
                " -ErrorAction SilentlyContinue"
            ),
            f"[bool]$task -and $task.Description -ne {marker}",
            "",
        )
    )
    queried = _run_powershell(query_script)
    return queried.stdout.strip() == "True"


def _windows_service_data_dir() -> Path | None:
    task_path = _powershell_literal("\\")
    task_name = _powershell_literal(WINDOWS_TASK_NAME.lstrip("\\"))
    query_script = "\n".join(
        (
            "$ErrorActionPreference = 'Stop'",
            f"$task = Get-ScheduledTask -TaskPath {task_path} -TaskName {task_name}",
            f"if ($task.Description -eq {_powershell_literal(MANAGED_MARKER)}) {{",
            "    $task.Actions.WorkingDirectory",
            "}",
            "",
        )
    )
    queried = _run_powershell(query_script, check=False)
    return (
        Path(queried.stdout.strip())
        if queried.returncode == 0 and queried.stdout.strip()
        else None
    )


def _install(context: SystemServiceContext, *, force: bool) -> list[str]:
    system = platform.system()
    if system == "Linux":
        return _install_linux(context, force=force)
    if system == "Darwin":
        return _install_macos(context, force=force)
    if system == "Windows":
        return _install_windows(context, force=force)
    raise RuntimeError(f"unsupported host service platform: {system}")


def _start_macos() -> None:
    plist_path, _ = _launchd_paths()
    if not plist_path.exists():
        raise RuntimeError(f"system service is not installed: {plist_path}")
    domain = _launchd_domain()
    target = f"{domain}/{LAUNCHD_LABEL}"
    loaded = _run_native_command(["launchctl", "print", target], check=False)
    if loaded.returncode == 0:
        _run_native_command(["launchctl", "kickstart", "-k", target])
    else:
        _run_native_command(["launchctl", "bootstrap", domain, str(plist_path)])


def _start_windows() -> None:
    _run_native_command(["schtasks", "/Run", "/TN", WINDOWS_TASK_NAME])


def _stop_linux() -> None:
    _run_native_command(["systemctl", "--user", "stop", SYSTEMD_UNIT_NAME])


def _stop_macos() -> None:
    _run_native_command(
        ["launchctl", "bootout", f"{_launchd_domain()}/{LAUNCHD_LABEL}"], check=False
    )


def _managed_windows_process_ids(context: SystemServiceContext) -> tuple[int, ...]:
    supervisor_path = _powershell_literal(context.data_dir / "supervisor.py")
    running_path = _powershell_literal(context.data_dir / "bcn.running.exe")
    script = "\n".join(
        (
            "$ErrorActionPreference = 'Stop'",
            "$queryProcessId = $PID",
            f"$supervisorPath = [IO.Path]::GetFullPath({supervisor_path}).ToLowerInvariant()",
            f"$runningPath = [IO.Path]::GetFullPath({running_path}).ToLowerInvariant()",
            "Get-CimInstance -ClassName Win32_Process |",
            "    Where-Object {",
            "        $_.ProcessId -ne $queryProcessId -and",
            "        $_.CommandLine -and",
            "        ($_.CommandLine.ToLowerInvariant().Contains($supervisorPath) -or",
            "         $_.CommandLine.ToLowerInvariant().Contains($runningPath))",
            "    } |",
            "    ForEach-Object { $_.ProcessId }",
            "",
        )
    )
    result = _run_powershell(script)
    process_ids: set[int] = set()
    for line in result.stdout.splitlines():
        value = line.strip()
        if value:
            process_ids.add(int(value))
    return tuple(sorted(process_ids))


def _stop_windows(context: SystemServiceContext) -> None:
    tracked_process_ids = set(_managed_windows_process_ids(context))
    _run_native_command(["schtasks", "/End", "/TN", WINDOWS_TASK_NAME], check=False)
    for _ in range(WINDOWS_STOP_ATTEMPTS):
        process_ids = set(_managed_windows_process_ids(context))
        tracked_process_ids.update(process_ids)
        for process_id in sorted(tracked_process_ids):
            _run_native_command(
                ["taskkill", "/PID", str(process_id), "/T", "/F"], check=False
            )
        task_status = _status_windows()
        if not process_ids and task_status.active is False:
            return
        time.sleep(WINDOWS_STOP_INTERVAL)
    raise RuntimeError(
        "Windows native service did not stop: "
        f"remaining_pids={','.join(str(pid) for pid in sorted(process_ids))} "
        f"detail={task_status.detail}"
    )


def _stop(context: SystemServiceContext) -> None:
    system = platform.system()
    if system == "Linux":
        _stop_linux()
    elif system == "Darwin":
        _stop_macos()
    elif system == "Windows":
        _stop_windows(context)
    else:
        raise RuntimeError(f"unsupported host service platform: {system}")


def _restart(context: SystemServiceContext) -> None:
    system = platform.system()
    if system == "Linux":
        _run_native_command(["systemctl", "--user", "restart", SYSTEMD_UNIT_NAME])
    elif system == "Darwin":
        _start_macos()
    elif system == "Windows":
        _stop_windows(context)
        _start_windows()
    else:
        raise RuntimeError(f"unsupported host service platform: {system}")


def _uninstall_linux() -> list[str]:
    unit_path = _systemd_unit_path()
    if not unit_path.exists():
        return ["system service is not installed"]
    _require_managed(unit_path)
    _run_native_command(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME])
    remove_managed_file(unit_path)
    _run_native_command(["systemctl", "--user", "daemon-reload"])
    return [f"system service uninstalled platform=linux path={unit_path}"]


def _uninstall_macos() -> list[str]:
    plist_path, wrapper_path = _launchd_paths()
    if not plist_path.exists() and not wrapper_path.exists():
        return ["system service is not installed"]
    _require_managed(plist_path, wrapper_path)
    _run_native_command(
        ["launchctl", "bootout", f"{_launchd_domain()}/{LAUNCHD_LABEL}"], check=False
    )
    remove_managed_file(plist_path)
    remove_managed_file(wrapper_path)
    return [f"system service uninstalled platform=macos path={plist_path}"]


def _uninstall_windows(context: SystemServiceContext) -> list[str]:
    xml_path, wrapper_path, launcher_path = _windows_paths(context.data_dir)
    _require_managed(xml_path, launcher_path, wrapper_path)
    if _windows_task_unmanaged():
        raise RuntimeError(
            f"refusing to remove unmanaged scheduled task: {WINDOWS_TASK_NAME}"
        )
    _stop_windows(context)
    _run_native_command(
        ["schtasks", "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"], check=False
    )
    remove_managed_file(xml_path)
    remove_managed_file(launcher_path)
    remove_managed_file(wrapper_path)
    if (
        not xml_path.exists()
        and not launcher_path.exists()
        and not wrapper_path.exists()
    ):
        return ["system service uninstalled platform=windows"]
    return []


def _uninstall(context: SystemServiceContext) -> list[str]:
    _require_managed(context.data_dir / "supervisor.py")
    system = platform.system()
    if system == "Linux":
        lines = _uninstall_linux()
    elif system == "Darwin":
        lines = _uninstall_macos()
    elif system == "Windows":
        lines = _uninstall_windows(context)
    else:
        raise RuntimeError(f"unsupported host service platform: {system}")
    remove_managed_file(context.data_dir / "supervisor.py")
    (
        context.data_dir / ("bcn.running.exe" if os.name == "nt" else "bcn.running")
    ).unlink(missing_ok=True)
    return lines


def _status_linux() -> NativeServiceStatus:
    unit_path = _systemd_unit_path()
    if not unit_path.exists():
        return NativeServiceStatus(False, False, False, "unit file is absent")
    enabled = _run_native_command(
        ["systemctl", "--user", "is-enabled", SYSTEMD_UNIT_NAME], check=False
    )
    active = _run_native_command(
        ["systemctl", "--user", "is-active", SYSTEMD_UNIT_NAME], check=False
    )
    return NativeServiceStatus(
        True,
        enabled.returncode == 0,
        active.returncode == 0,
        f"enabled={enabled.stdout.strip() or 'unknown'} "
        f"active={active.stdout.strip() or 'unknown'}",
    )


def _status_macos() -> NativeServiceStatus:
    plist_path, _ = _launchd_paths()
    if not plist_path.exists():
        return NativeServiceStatus(False, False, False, "plist is absent")
    loaded = _run_native_command(
        ["launchctl", "print", f"{_launchd_domain()}/{LAUNCHD_LABEL}"], check=False
    )
    if loaded.returncode != 0:
        return NativeServiceStatus(True, True, False, "launchd job is not loaded")
    state = _launchd_state(loaded.stdout)
    active = state == "running" if state is not None else None
    detail = f"launchd state={state}" if state is not None else "launchd job"
    return NativeServiceStatus(True, True, active, detail)


def _launchd_state(output: str) -> str | None:
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "state":
            return value.strip().casefold()
    return None


def _status_windows() -> NativeServiceStatus:
    task_path = _powershell_literal("\\")
    task_name = _powershell_literal(WINDOWS_TASK_NAME.lstrip("\\"))
    query_script = "\n".join(
        (
            "$ErrorActionPreference = 'Stop'",
            f"$task = Get-ScheduledTask -TaskPath {task_path} -TaskName {task_name}",
            "$task.State.ToString().ToLowerInvariant()",
            "",
        )
    )
    queried = _run_powershell(query_script, check=False)
    installed = queried.returncode == 0
    active = _windows_task_active_state(queried.stdout) if installed else False
    detail = "task is absent"
    if installed:
        detail = "registered"
        if active is not None:
            detail = f"state={'running' if active else 'ready'}"
    return NativeServiceStatus(installed, installed, active, detail)


def _windows_task_active_state(output: str) -> bool | None:
    running_states = {"running", "运行中", "正在运行"}
    inactive_states = {"disabled", "queued", "ready", "已禁用", "就绪", "排队"}
    for line in output.splitlines():
        state = line.strip().casefold()
        if state in running_states:
            return True
        if state in inactive_states:
            return False
        key, separator, value = line.partition(":")
        if not separator or key.strip().casefold() not in {"status", "task state"}:
            continue
        state = value.strip().casefold()
        if state in running_states:
            return True
        if state in inactive_states:
            return False
    return None


def _native_status() -> NativeServiceStatus:
    system = platform.system()
    if system == "Linux":
        return _status_linux()
    if system == "Darwin":
        return _status_macos()
    if system == "Windows":
        return _status_windows()
    raise RuntimeError(f"unsupported host service platform: {system}")


class SystemService:
    """Run one system-service command for the node this invocation configures."""

    def __init__(self, configuration: Configuration, client: Client, timer: Timer):
        self.configuration = configuration
        self.client = client
        self.timer = timer

    def node_options(self) -> tuple[str, ...]:
        """The configer selection of this invocation, which the service repeats."""

        assert self.configuration.environment is not None
        configer = self.configuration.environment.entries[0]
        options: list[str] = ["--configer", configer.entry]
        for option in configer.config.get("options", ()):
            options += ["--configer-options", str(option)]
        return tuple(options)

    async def health(self) -> str:
        try:
            ready = await self.client.call(
                "system-service-readiness", "ready", timeout=0.5
            )
        except RemoteError:
            return "unhealthy"
        except OSError:
            return "unreachable"
        return "ready" if ready is True else "not-ready"

    async def wait_until_ready(self) -> str:
        deadline = self.timer.now_ms + READY_WAIT_MS
        while self.timer.now_ms < deadline:
            health = await self.health()
            if health == "ready":
                return health
            await self.timer.create(50).wait()
        return await self.health()

    async def _check_endpoint_conflict(
        self, native_status: NativeServiceStatus
    ) -> str | None:
        if native_status.active is not True and await self.health() == "ready":
            return "endpoint is healthy while the native service is inactive"
        return None

    async def _wait_for_managed_service_health(self) -> str:
        health = await self.wait_until_ready()
        if health != "ready":
            return health
        final_status = await asyncio.to_thread(_native_status)
        if final_status.active is not True:
            return f"native service is not active ({final_status.detail})"
        return health

    async def _require_installed(self) -> NativeServiceStatus:
        native_status = await asyncio.to_thread(_native_status)
        if not native_status.installed:
            raise click.UsageError(NOT_INSTALLED)
        return native_status

    async def run(
        self, command: str, *, env_file: Path | None = None, force: bool = False
    ) -> None:
        try:
            context = await asyncio.to_thread(
                _build_context,
                self.node_options() if command == "install" else (),
                env_file,
                require_executable=command == "install",
            )
            lines: list[str] = []
            if command == "install":
                await asyncio.to_thread(
                    context.data_dir.mkdir, parents=True, exist_ok=True
                )
                lines = await asyncio.to_thread(_install, context, force=force)
            elif command in ("start", "restart"):
                native_status = await self._require_installed()
                conflict = await self._check_endpoint_conflict(native_status)
                if conflict is not None:
                    raise click.UsageError(
                        f"system service did not {command}: {conflict}"
                    )
                # a running service starts over, so what was just configured
                # is what runs
                await asyncio.to_thread(_restart, context)
                health = await self._wait_for_managed_service_health()
                if health != "ready":
                    raise click.UsageError(
                        f"system service did not become ready: {health}"
                    )
                done = "started" if command == "start" else "restarted"
                lines = [
                    (
                        f"system service {done} platform={platform.system().lower()} "
                        "health=ready"
                    )
                ]
            elif command == "stop":
                await self._require_installed()
                await asyncio.to_thread(_stop, context)
                lines = [f"system service stopped platform={platform.system().lower()}"]
            elif command == "uninstall":
                lines = await asyncio.to_thread(_uninstall, context)
            elif command == "status":
                native_status = await asyncio.to_thread(_native_status)
                health = await self.health()
                lines = [
                    (
                        f"platform={platform.system().lower()} "
                        f"installed={'yes' if native_status.installed else 'no'} "
                        f"enabled={'yes' if native_status.enabled else 'no'} "
                        f"active={'yes' if native_status.active else 'no'} "
                        f"health={health} detail={native_status.detail}"
                    )
                ]
        except (OSError, RuntimeError, ValueError) as error:
            raise click.UsageError(str(error)) from error
        for line in lines:
            await asyncio.to_thread(click.echo, line)


__all__ = [
    "LAUNCHD_LABEL",
    "MANAGED_MARKER",
    "SYSTEMD_UNIT_NAME",
    "WINDOWS_TASK_NAME",
    "NativeServiceStatus",
    "SystemService",
    "SystemServiceContext",
    "default_env_file",
    "remove_managed_file",
    "render_launchd_plist",
    "render_launchd_wrapper",
    "render_systemd_unit",
    "render_windows_launcher",
    "render_windows_task",
    "render_windows_wrapper",
    "resolve_data_dir",
    "resolve_service_data_dir",
    "write_managed_file",
]

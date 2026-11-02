# Managed by bazaar-compute-node.
"""Keep the service's running entry point separate from its installed entry point."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from types import FrameType


def refresh(executable: Path, running: Path) -> None:
    descriptor, name = tempfile.mkstemp(dir=running.parent, prefix=f".{running.name}.")
    os.close(descriptor)
    fresh: Path | None = Path(name)
    try:
        shutil.copyfile(executable, fresh)
        fresh.chmod(0o700)
        os.replace(fresh, running)
        fresh = None
    finally:
        if fresh is not None:
            fresh.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--configer", required=True)
    parser.add_argument("--configer-options", action="append", default=[])
    args = parser.parse_args()
    options = ["--configer", args.configer]
    for option in args.configer_options:
        options += ["--configer-options", option]
    running = Path(__file__).with_name(
        "bcn.running.exe" if os.name == "nt" else "bcn.running"
    )
    stopping = False
    process: subprocess.Popen[bytes] | None = None

    def stop(number: int, frame: FrameType | None) -> None:
        nonlocal stopping
        del number, frame
        stopping = True
        if process is not None and process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while not stopping:
        try:
            refresh(args.executable, running)
            if stopping:
                break
            process = subprocess.Popen(
                [str(running), "run", *options],
                stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            print(f"system service supervisor started pid={process.pid}", flush=True)
            if stopping:
                process.terminate()
            code = process.wait()
            print(f"system service supervisor child exited code={code}", flush=True)
        except OSError as error:
            print(f"system service supervisor launch failed: {error}", flush=True)
        if not stopping:
            time.sleep(1)


if __name__ == "__main__":
    main()

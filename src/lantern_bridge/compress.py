# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""Compressing and decompressing the packages of a folder open to changes, with the ``nsz`` tool.

The bridge does not compress anything itself: it runs ``nsz`` (a separate tool, with the console keys the user
puts in the configuration folder as ``prod.keys``), one job at a time, in a work folder inside the source's
folder (so the result is moved next to the original by a rename, on the same volume). The original is kept unless
the user asks for it to go, and then only after the tool finished, verified the NCAs (``-Q``) and the new file
is there. A job that fails, or is cancelled, leaves the folder as it was.

Nothing here reads the keys: they are the tool's.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lantern_bridge.runtime import Runtime

log = logging.getLogger("lantern_bridge.compress")

#: What a package can become: its new format, and whether that is compressing or the other way.
CONVERSIONS = {
    "nsp": ("nsz", "compress"),
    "xci": ("xcz", "compress"),
    "nsz": ("nsp", "decompress"),
    "xcz": ("xci", "decompress"),
}
#: A folder this bridge works in, inside the source's folder; the pass that reads the folder leaves it alone.
WORK = ".lantern-work-"
KEEP = 50  # jobs remembered
LEVEL = 18  # nsz's own default


def argv(tool: str) -> list[str]:
    """The command for a tool: a Python script (a stand-in for tests, or a tool run with the same Python) runs with
    this interpreter, anything else is run as it is."""
    return [sys.executable, tool] if tool.endswith(".py") else [tool]


def find_tool() -> str | None:
    """The ``nsz`` command, if it is installed (on the path, or next to this Python)."""
    return shutil.which("nsz") or shutil.which("nsz", path=str(Path(sys.executable).parent))


@dataclass(slots=True)
class Job:
    id: int
    source: str
    path: str  # inside the source's folder, with "/"
    title_id: str
    name: str
    action: str  # "compress" | "decompress"
    target: str  # the format it becomes: nsz, xcz, nsp, xci
    level: int = LEVEL
    block: bool = False  # nsz's block mode (the default of XCZ), not the solid one
    delete_original: bool = False
    state: str = "queued"  # queued | running | done | failed | cancelled
    message: str = ""
    size_in: int = 0
    size_out: int = 0
    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    cancel: bool = False


class Compressor:
    """The jobs, and the one thread that runs them. It outlives a settings reload: each new runtime is attached."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.runtime: Runtime | None = None
        self._jobs: deque[Job] = deque(maxlen=KEEP)
        self._queue: deque[Job] = deque()
        self._ids = 0
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[str] | None = None
        self._closing = False

    def attach(self, runtime: Runtime) -> None:
        self.runtime = runtime

    # -- what is needed --------------------------------------------------------------------------

    def status(self) -> str:
        """`ready`, or what is missing: `no_tool` (nsz is not installed) or `no_keys` (no prod.keys)."""
        if find_tool() is None:
            return "no_tool"
        if not any((self.state_dir / name).is_file() for name in ("prod.keys", "keys.txt")):
            return "no_keys"
        return "ready"

    # -- the jobs --------------------------------------------------------------------------------

    def jobs(self) -> list[Job]:
        with self._lock:
            return list(reversed(self._jobs))

    def busy(self) -> bool:
        with self._lock:
            return any(j.state in ("queued", "running") for j in self._jobs)

    def submit(
        self,
        source: str,
        path: str,
        title_id: str,
        *,
        level: int = LEVEL,
        block: bool = False,
        delete_original: bool = False,
    ) -> Job:
        """Queues a job for a package; ValueError for a format that cannot be converted."""
        suffix = path.rsplit(".", 1)[-1].lower()
        if suffix not in CONVERSIONS:
            raise ValueError("This format cannot be converted.")
        target, action = CONVERSIONS[suffix]
        with self._lock:
            self._ids += 1
            job = Job(
                self._ids,
                source,
                path,
                title_id,
                path.rsplit("/", 1)[-1],
                action,
                target,
                level=min(max(int(level), 1), 22),
                block=block and action == "compress",
                delete_original=delete_original,
            )
            self._jobs.append(job)
            self._queue.append(job)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._work, name="lantern-bridge-compress", daemon=True)
                self._thread.start()
            self._wake.notify()
        self._changed()
        return job

    def cancel(self, job_id: int) -> bool:
        with self._lock:
            job = next((j for j in self._jobs if j.id == job_id), None)
            if job is None or job.state not in ("queued", "running"):
                return False
            job.cancel = True
            if job.state == "queued":
                job.state, job.finished, job.message = "cancelled", time.time(), "Cancelled before it started."
                with contextlib.suppress(ValueError):
                    self._queue.remove(job)
            elif self._process is not None:
                self._process.terminate()
        self._changed()
        return True

    def close(self) -> None:
        """Ends the running job (the folder is left as it was) and the thread."""
        with self._lock:
            self._closing = True
            for job in self._queue:
                job.state, job.finished, job.message = "cancelled", time.time(), "The bridge stopped."
            self._queue.clear()
            for job in self._jobs:
                if job.state == "running":
                    job.cancel = True
            if self._process is not None:
                self._process.terminate()
            self._wake.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=15)

    # -- the thread ------------------------------------------------------------------------------

    def _changed(self) -> None:
        if self.runtime is not None:
            self.runtime.pulse.bump()

    def _work(self) -> None:
        while True:
            with self._lock:
                while not self._queue and not self._closing:
                    if not self._wake.wait(timeout=10) and not self._queue:
                        self._thread = None  # idle: the next submit starts a thread again
                        return
                if self._closing:
                    return
                job = self._queue.popleft()
                if job.cancel:
                    continue
                job.state, job.started = "running", time.time()
            self._changed()
            try:
                self._run(job)
            except Exception as error:
                log.exception("a compression job failed")
                self._finish(job, "failed", f"{type(error).__name__}: {error}")

    def _finish(self, job: Job, state: str, message: str = "") -> None:
        with self._lock:
            job.state, job.message, job.finished = state, message, time.time()
        self._changed()

    def _run(self, job: Job) -> None:
        runtime, tool = self.runtime, find_tool()
        if runtime is None or tool is None:
            self._finish(job, "failed", "The nsz tool is not installed.")
            return
        root = runtime.roots.get(job.source)
        source = root.joinpath(*job.path.split("/")) if root is not None else None
        if root is None or source is None or not source.is_file():
            self._finish(job, "failed", "The file is not there any more.")
            return
        job.size_in = source.stat().st_size
        need = job.size_in * (2 if job.action == "decompress" else 1)
        if shutil.disk_usage(root).free < need:
            self._finish(job, "failed", "Not enough free space in the folder for the result.")
            return
        work = source.parent / f"{WORK}{job.id}"
        work.mkdir(exist_ok=True)
        try:
            ok, why = self._tool(job, tool, source, work)
            if job.cancel:
                self._finish(job, "cancelled", "Cancelled.")
                return
            if not ok:
                self._finish(job, "failed", why)
                return
            made = sorted(p for p in work.iterdir() if p.is_file() and p.suffix.lower() == f".{job.target}")
            if len(made) != 1 or made[0].stat().st_size == 0:
                self._finish(job, "failed", "The tool made no file.")
                return
            final = source.with_name(made[0].name)
            if final.exists():
                self._finish(job, "failed", "A file of that name is already there: nothing was replaced.")
                return
            job.size_out = made[0].stat().st_size
            os.replace(made[0], final)  # the same volume: a rename
        finally:
            shutil.rmtree(work, ignore_errors=True)
        removed = False
        if job.delete_original:
            try:
                source.unlink()
                removed = True
            except OSError as error:
                log.warning("the original of a compressed file could not be deleted: %s", error.strerror or error)
        runtime.history.event("converted", subject=job.title_id, detail=f"{source.suffix[1:].lower()}→{job.target}")
        if removed:
            runtime.forget_file(job.source, job.path)
        runtime.rescan()
        self._finish(job, "done", "" if removed or not job.delete_original else "The original could not be deleted.")

    def _tool(self, job: Job, tool: str, source: Path, work: Path) -> tuple[bool, str]:
        """Runs nsz; (True, "") when it finished well, else (False, why)."""
        command = [*argv(tool), "-C" if job.action == "compress" else "-D", "--machine-readable"]
        command += ["--keys", str(self.state_dir)]
        command += ["-o", str(work), "-m", "1"]
        if job.action == "compress":
            command += ["-l", str(job.level), "-B" if job.block else "-S"]
        if job.delete_original:
            command += ["-Q"]  # the original goes only after the NCAs were verified
        command.append(str(source))
        process = subprocess.Popen(  # noqa: S603  (the command is built here; the file is an argument, never a shell word)
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(work)
        )
        with self._lock:
            self._process = process
            if job.cancel:  # asked to stop while the tool was being started: the request found no process yet
                process.terminate()
        summary: dict[str, Any] = {}
        errors: list[str] = []

        def read() -> None:
            assert process.stdout is not None  # noqa: S101
            for line in process.stdout:
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if isinstance(data, dict) and data.get("type") == "summary":
                    summary.update(data)
                elif isinstance(data, dict) and data.get("type") in ("warning", "error"):
                    errors.append(str(data.get("message", ""))[:200])

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        while process.poll() is None:
            if job.cancel:
                process.terminate()
            job.size_out = sum(p.stat().st_size for p in work.iterdir() if p.is_file())  # the progress
            time.sleep(0.5)
        reader.join(timeout=5)
        with self._lock:
            self._process = None
        if job.cancel:
            return False, "Cancelled."
        if process.returncode == 0 and summary.get("success", True):
            return True, ""
        first = (summary.get("errors") or errors or ["The tool failed."])[0]
        return False, str(first)[:200]

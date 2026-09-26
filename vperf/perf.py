"""Thin wrapper around the perf binary."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from typing import TextIO


class PerfError(RuntimeError):
    pass


PERF = shutil.which("perf") or "/usr/bin/perf"


def _perf_env() -> dict[str, str]:
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LC_NUMERIC"] = "C"
    env.pop("DEBUGINFOD_URLS", None)
    return env


def perf_available() -> bool:
    return shutil.which("perf") is not None


def perf_version() -> str:
    r = subprocess.run(
        [PERF, "--version"], capture_output=True, text=True, timeout=10,
        env=_perf_env(),
    )
    return (r.stdout or r.stderr).strip().splitlines()[-1] if r.returncode == 0 else "?"


@dataclass
class PerfResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class PerfProcess:
    process: subprocess.Popen
    args: list[str]
    _stdout: TextIO
    _stderr: TextIO
    _stdout_is_file: bool = False

    def poll(self) -> int | None:
        return self.process.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self.process.wait(timeout=timeout)

    def result(self, timeout: float | None = None) -> PerfResult:
        returncode = self.wait(timeout=timeout)
        out = ""
        if not self._stdout_is_file:
            # a file-backed stdout is an artifact, not something to read back:
            # perf script alone writes hundreds of MB
            self._stdout.seek(0)
            out = self._stdout.read() or ""
        self._stderr.seek(0)
        return PerfResult(returncode, out, self._stderr.read() or "")

    def close(self) -> None:
        for stream in (self._stdout, self._stderr):
            try:
                stream.close()
            except OSError:
                pass

    def reap(self, grace: float = 2.0) -> None:
        """Make sure the child is gone, escalating until it is.

        SIGINT first: perf treats it as "stop and write what you have", so a
        dump that overran keeps the samples it managed to produce.  Unlike
        stop() the returncode is left alone -- the caller is reporting a
        timeout, not a clean finish.
        """
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            if self.poll() is not None:
                return
            try:
                self.process.send_signal(sig)
            except OSError:
                return
            try:
                self.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                continue
        try:
            self.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass

    def join(self, timeout: float | None = None) -> PerfResult:
        """Wait for a deferred child, killing it if it overruns `timeout`.

        The post-target dumps are started before they are waited on, so a
        timeout has to reap the child: a leaked `perf script` would keep
        appending to its artifact after the report was built, and a leaked
        `perf mem report` would race the retry that reopens the same file.
        """
        try:
            return self.result(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.reap()
            return PerfResult(
                -1, "", f"perf timed out after {timeout}s: {' '.join(self.args)}")

    def stop(self, grace: float = 2.0) -> PerfResult:
        interrupted = False
        if self.poll() is None:
            try:
                self.process.send_signal(signal.SIGINT)
                interrupted = True
            except OSError:
                pass
            if self.poll() is None:
                try:
                    self.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    interrupted = False
                    try:
                        self.process.terminate()
                    except OSError:
                        pass
                    if self.poll() is None:
                        try:
                            self.process.kill()
                        except OSError:
                            pass
                        self.wait()
        result = self.result()
        if interrupted and result.returncode in (-signal.SIGINT, 128 + signal.SIGINT):
            result.returncode = 0
        return result


def start_perf(args: list[str], stdout_file: str | None = None) -> PerfProcess:
    """Start `perf <args>` in the background.

    With stdout_file the child writes straight to that path, so a large dump
    never passes through this process; result() then reports stderr only.
    """
    if stdout_file:
        stdout: TextIO = open(stdout_file, "w", encoding="utf-8")
    else:
        stdout = tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace")
    stderr = tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace")
    try:
        process = subprocess.Popen(
            [PERF, *args],
            stdout=stdout,
            stderr=stderr,
            text=True,
            env=_perf_env(),
        )
    except Exception:
        stdout.close()
        stderr.close()
        raise
    return PerfProcess(process, list(args), stdout, stderr,
                       _stdout_is_file=stdout_file is not None)


def run_perf(
    args: list[str],
    timeout: float | None = None,
    stdout_file: str | None = None,
    defer: bool = False,
) -> PerfResult | PerfProcess:
    """Run `perf <args>` and capture output.

    With defer=True the process is started and a PerfProcess returned instead
    of a PerfResult: the caller joins it later, so independent perf
    invocations can overlap.  stdout_file still receives the output either way.
    """
    if defer:
        return start_perf(args, stdout_file=stdout_file)
    env = _perf_env()
    cmd = [PERF, *args]
    fout = open(stdout_file, "w", encoding="utf-8") if stdout_file else subprocess.PIPE
    try:
        r = subprocess.run(
            cmd,
            stdout=fout,
            stderr=subprocess.PIPE,
            timeout=timeout,
            text=(stdout_file is None),
            errors="replace",
            env=env,
        )
        out = "" if stdout_file else r.stdout or ""
        err = r.stderr or ""
        return PerfResult(r.returncode, out, err)
    except FileNotFoundError as e:
        raise PerfError(f"perf binary not found: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise PerfError(f"perf timed out after {timeout}s: {' '.join(cmd)}") from e
    finally:
        if stdout_file:
            fout.close()

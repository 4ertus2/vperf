"""Thin wrapper around the perf binary."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


class PerfError(RuntimeError):
    pass


PERF = shutil.which("perf") or "/usr/bin/perf"


def perf_available() -> bool:
    return shutil.which("perf") is not None


def perf_version() -> str:
    r = subprocess.run(
        [PERF, "--version"], capture_output=True, text=True, timeout=10
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


def run_perf(
    args: list[str],
    timeout: float | None = None,
    stdout_file: str | None = None,
) -> PerfResult:
    """Run `perf <args>` and capture output."""
    import os

    env = dict(os.environ)
    env["LC_ALL"] = "C"          # force '.' decimal separators in CSV output
    env["LC_NUMERIC"] = "C"
    # prevent perf from blocking on debuginfod network fetches during
    # unwinding/reporting (a common multi-second hang).
    # NOTE: an *empty* string still triggers Ubuntu's compiled-in default URL,
    # so the variable must be removed entirely.
    env.pop("DEBUGINFOD_URLS", None)
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

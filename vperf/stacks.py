"""Aggregate perf script samples into VTune-style hotspot structures."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .parsers import ScriptSample, sanitize_symbol


@dataclass
class Hotspot:
    name: str
    dso: str
    self_cycles: int = 0
    self_pct: float = 0.0
    total_cycles: int = 0      # inclusive
    est_cpu_time: float | None = None   # seconds, scaled from task-clock


@dataclass
class ThreadInfo:
    tid: int
    pid: int
    comm: str
    cycles: int = 0


@dataclass
class TreeNode:
    name: str
    value: int = 0                 # inclusive cycles under this node
    children: dict[str, TreeNode] = field(default_factory=dict)


@dataclass
class StackProfile:
    total_cycles: int = 0
    samples: int = 0
    hotspots: list[Hotspot] = field(default_factory=list)
    by_thread: dict[int, ThreadInfo] = field(default_factory=dict)
    by_dso: dict[str, int] = field(default_factory=dict)          # self cycles
    folded: dict[str, int] = field(default_factory=dict)          # "root;a;b;c" -> cycles
    call_tree: TreeNode | None = None
    time_range: tuple[float, float] | None = None                  # first/last sample ts


_TRANSIENT_COMM = {"perf-exec", "perf", "?", "", "[unknown]"}


def _is_transient(comm: str) -> bool:
    return comm in _TRANSIENT_COMM or comm.startswith("perf-")


def _build_call_tree(chains: dict[tuple[str, ...], int]) -> TreeNode:
    root = TreeNode(name="all")
    for chain, w in chains.items():
        node = root
        node.value += w
        for fname in chain:
            node.children.setdefault(fname, TreeNode(name=fname))
            node = node.children[fname]
            node.value += w
    return root


def build_profile(samples: list[ScriptSample]) -> StackProfile:
    prof = StackProfile()
    self_by_func: dict[str, int] = defaultdict(int)
    dso_by_func: dict[str, str] = {}
    incl_by_func: dict[str, int] = defaultdict(int)
    chains: dict[tuple[str, ...], int] = defaultdict(int)

    tmin: float | None = None
    tmax: float | None = None

    for s in samples:
        w = s.period
        prof.total_cycles += w
        prof.samples += 1
        if tmin is None or s.time < tmin:
            tmin = s.time
        if tmax is None or s.time > tmax:
            tmax = s.time

        ti = prof.by_thread.get(s.tid)
        if ti is None:
            ti = ThreadInfo(tid=s.tid, pid=s.pid, comm=s.comm)
            prof.by_thread[s.tid] = ti
        elif ti.comm != s.comm and not _is_transient(s.comm):
            ti.comm = s.comm
        ti.cycles += w

        # perf prints leaf-first; normalize to caller->leaf
        callers = [sanitize_symbol(sym) for sym, _dso in reversed(s.frames)]
        leaf_dso = s.frames[0][1] if s.frames else "[unknown]"

        if not callers:
            callers = ["[unknown]"]

        leaf = callers[-1]
        self_by_func[leaf] += w
        dso_by_func.setdefault(leaf, leaf_dso)

        seen: set[str] = set()
        for f in callers:
            if f in seen:          # recursion: count inclusive once per sample
                continue
            seen.add(f)
            incl_by_func[f] += w
        prof.by_dso[leaf_dso] = prof.by_dso.get(leaf_dso, 0) + w

        key_root = f"{s.comm} ({s.pid})"
        chain = (key_root, *callers)
        chains[chain] += w
        prof.folded[";".join(chain)] = prof.folded.get(";".join(chain), 0) + w

    total = max(prof.total_cycles, 1)

    # normalize transient comm labels (perf-exec) across folded keys/threads
    real_comm = {}
    for s in samples:
        if not _is_transient(s.comm):
            real_comm[s.tid] = s.comm
    renames: dict[str, str] = {}
    for tid, ti in prof.by_thread.items():
        real = real_comm.get(tid, ti.comm)
        if real != ti.comm:
            renames[f"{ti.comm} ({ti.pid})"] = f"{real} ({ti.pid})"
            ti.comm = real
    if renames:
        def fix(key: str) -> str:
            head, *tailr = key.split(";")
            return ";".join([renames.get(head, head), *tailr])
        new_folded: dict[str, int] = {}
        for k, v in prof.folded.items():
            nk = fix(k)
            new_folded[nk] = new_folded.get(nk, 0) + v
        prof.folded = new_folded
        new_chains: dict[tuple, int] = {}
        for k, v in chains.items():
            nk = fix(k)
            new_chains[nk] = new_chains.get(nk, 0) + v
        chains = new_chains

    rows: dict[str, Hotspot] = {}
    for fname, sc in self_by_func.items():
        rows[fname] = Hotspot(name=fname, dso=dso_by_func.get(fname, "[unknown]"), self_cycles=sc)
    for fname, ic in incl_by_func.items():
        row = rows.setdefault(fname, Hotspot(name=fname, dso=dso_by_func.get(fname, "[unknown]")))
        if not row.dso or row.dso == "[unknown]":
            row.dso = dso_by_func.get(fname, row.dso)
        row.total_cycles = ic
    for r in rows.values():
        r.self_pct = r.self_cycles / total * 100.0
    prof.hotspots = sorted(rows.values(), key=lambda h: h.self_cycles, reverse=True)
    prof.call_tree = _build_call_tree(chains)
    if tmin is not None and tmax is not None:
        prof.time_range = (tmin, tmax)
    return prof


def scale_hotspot_times(prof: StackProfile, cpu_time_sec: float | None) -> None:
    """Attach estimated CPU seconds using global task-clock accounting."""
    if not cpu_time_sec or prof.total_cycles == 0:
        return
    for h in prof.hotspots:
        h.est_cpu_time = cpu_time_sec * h.self_cycles / prof.total_cycles


def top_threads(prof: StackProfile, n: int = 12) -> list[ThreadInfo]:
    return sorted(prof.by_thread.values(), key=lambda t: t.cycles, reverse=True)[:n]

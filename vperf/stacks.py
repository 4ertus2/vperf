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
class UserStackView:
    total_cycles: int = 0
    samples: int = 0
    folded: dict[str, int] = field(default_factory=dict)
    folded_by_tid: dict[int, dict[str, int]] = field(default_factory=dict)
    call_tree: TreeNode | None = None


@dataclass
class StackProfile:
    total_cycles: int = 0
    samples: int = 0
    hotspots: list[Hotspot] = field(default_factory=list)
    by_thread: dict[int, ThreadInfo] = field(default_factory=dict)
    by_dso: dict[str, int] = field(default_factory=dict)          # self cycles
    folded: dict[str, int] = field(default_factory=dict)          # "root;a;b;c" -> cycles
    folded_by_tid: dict[int, dict[str, int]] = field(default_factory=dict)
    call_tree: TreeNode | None = None
    time_range: tuple[float, float] | None = None                  # first/last sample ts
    user_stacks: UserStackView = field(default_factory=UserStackView)


_TRANSIENT_COMM = {"perf-exec", "perf", "?", "", "[unknown]"}
_KERNEL_BOUNDARY = "[kernel boundary]"
# perf's own default call-graph depth; see cap_stacks for why the recorded
# stacks are not trusted to come back sane.
MAX_STACK_FRAMES = 128


def cap_stacks(samples: list[ScriptSample], limit: int = MAX_STACK_FRAMES) -> list[ScriptSample]:
    """Drop the caller-side tail of every sample, keeping the leaf end.

    A target that does not preserve frame pointers leaves the unwinder walking
    stale stack memory, and an attached record can come back with thousands of
    frames per sample - all of them past the real outermost frame.  The leaf
    side is what self-time attribution, the folded keys and the flame graph
    need, so the tail is what goes.
    """
    out: list[ScriptSample] = []
    for s in samples:
        if len(s.frames) > limit:
            s.frames = s.frames[:limit]
        out.append(s)
    return out


def _is_transient(comm: str) -> bool:
    return comm in _TRANSIENT_COMM or comm.startswith("perf-")


def _frame_domain(sym: str, dso: str) -> str:
    normalized = dso.lower()
    if (
        sym == "[kernel]"
        or normalized.startswith("[kernel")
        or normalized in {"kernel", "vmlinux", "bpf", "[bpf]"}
        or normalized.endswith(".ko")
        or "/lib/modules/" in normalized
        or normalized.startswith("/sys/kernel/")
    ):
        return "kernel"
    if normalized in {"inlined", "(inlined)"}:
        return "inline"
    if not dso or normalized in {"[unknown]", "[unresolved]"}:
        return "unknown"
    return "user"


def _user_stack_frames(frames: list[tuple[str, str]]) -> tuple[list[str], bool]:
    user_frames: list[str] = []
    pending_inline: list[str] = []
    has_kernel = False
    last_domain = "unknown"

    for sym, dso in frames:
        domain = _frame_domain(sym, dso)
        if domain == "inline":
            pending_inline.append(sym)
            continue
        if domain == "kernel":
            has_kernel = True
            pending_inline.clear()
        elif domain == "user":
            user_frames.extend(pending_inline)
            pending_inline.clear()
            user_frames.append(sym)
        else:
            pending_inline.clear()
        last_domain = domain

    if pending_inline and last_domain == "user":
        user_frames.extend(pending_inline)
    return user_frames, has_kernel


def _rename_folded_roots(
    folded: dict[str, int], chains: dict[tuple[str, ...], int], renames: dict[str, str],
) -> tuple[dict[str, int], dict[tuple[str, ...], int]]:
    if not renames:
        return folded, chains

    def fix(key: str) -> str:
        head, *tail = key.split(";")
        return ";".join([renames.get(head, head), *tail])

    new_folded: dict[str, int] = {}
    for key, value in folded.items():
        renamed = fix(key)
        new_folded[renamed] = new_folded.get(renamed, 0) + value
    new_chains: dict[tuple[str, ...], int] = {}
    for chain, value in chains.items():
        renamed = tuple(fix(";".join(chain)).split(";"))
        new_chains[renamed] = new_chains.get(renamed, 0) + value
    return new_folded, new_chains


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
    user = prof.user_stacks
    user_chains: dict[tuple[str, ...], int] = defaultdict(int)

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

        # perf prints leaf-first; normalize to caller->leaf.  The same cap as
        # cap_stacks(), which the CLI applies up front - repeated here so a
        # direct caller of build_profile() cannot build an unbounded tree.
        frames = s.frames[:MAX_STACK_FRAMES]
        callers = [sanitize_symbol(sym) for sym, _dso in reversed(frames)]
        leaf_dso = frames[0][1] if frames else "[unknown]"

        if not callers:
            callers = ["[unknown]"]

        thread_folded = prof.folded_by_tid.setdefault(s.tid, {})
        thread_key = ";".join(callers)
        thread_folded[thread_key] = thread_folded.get(thread_key, 0) + w

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

        user_frames, has_kernel = _user_stack_frames(frames)
        if user_frames or has_kernel:
            user_callers = [sanitize_symbol(sym) for sym in reversed(user_frames)]
            if has_kernel or not user_callers:
                user_callers.append(_KERNEL_BOUNDARY)
            user.total_cycles += w
            user.samples += 1
            user_thread_folded = user.folded_by_tid.setdefault(s.tid, {})
            user_thread_key = ";".join(user_callers)
            user_thread_folded[user_thread_key] = user_thread_folded.get(user_thread_key, 0) + w
            user_chain = (key_root, *user_callers)
            user_chains[user_chain] += w
            user.folded[";".join(user_chain)] = user.folded.get(";".join(user_chain), 0) + w

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
        prof.folded, chains = _rename_folded_roots(prof.folded, chains, renames)
        user.folded, user_chains = _rename_folded_roots(user.folded, user_chains, renames)

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
    user.call_tree = _build_call_tree(user_chains) if user_chains else None
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

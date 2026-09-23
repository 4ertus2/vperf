"""Memory-access analysis via AMD IBS or Intel PEBS.

Collection uses one of two backends depending on hardware:

AMD IBS (Instruction-Based Sampling):
    perf record -d -W -e ibs_op//p -c <period> -- <target>

Intel PEBS (Precise Event-Based Sampling):
    perf mem record --ldlat 30 -- <target>

Both produce ``perf mem report`` output with per-sample cache-level
classification and latency data, parsed identically by this module.

The parser classifies every sampled load/store by where its data came
from (L1/L2/L3/RAM) together with the access latency in cycles.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# VTune-style latency bands, in cycles
LATENCY_BANDS = [
    ("<=50", 0, 50),
    ("51-100", 50, 100),
    ("101-200", 100, 200),
    ("201-500", 200, 500),
    ("501-1k", 500, 1000),
    ("1k-2k", 1000, 2000),
    (">2k", 2000, float("inf")),
]

_ROW_SPLIT = re.compile(r"\s{2,}")
_EVENT_SAMPLES = re.compile(r"#\s*Samples:.*event ['\"]([^'\"]+)")

_SKIP_PREFIXES = (
    "#", "Warning:", "Kernel address", "Check ", "As no ",
    "Samples in kernel", "can't be resolved",
)

_HEADER_HINTS = {
    "overhead": None,
    "samples": None,
    "local weight": None,
    "period": None,
    "memory access": None,
    "symbol": None,
    "shared object": None,
    "data object": None,
    "tlb access": None,
    "tgid:command": None,
    "pid:command": None,
    "command": None,
}


def _classify_access(access: str) -> str:
    a = access.strip().lower()
    if not a or a == "n/a":
        return "unclassified"
    if "l1" in a or "lfb" in a:
        return "L1"
    if "l2" in a:
        return "L2"
    if "l3" in a:
        return "L3"
    if "ram" in a or "dram" in a or "io" in a or "memory" in a:
        return "DRAM"
    return "other"


def _clean_symbol(sym: str) -> str:
    """'[.] main' -> 'main'; '[k] 0x...' -> '[kernel]'"""
    s = sym.strip()
    if s.startswith("[k]"):
        return "[kernel]"
    if s.startswith(("[.]", "[u]", "[T]")):
        s = s[3:].strip()
    return s or "[unknown]"


def _int_value(value: str, default: int = 0) -> int:
    try:
        return int(float(value.replace(",", "")))
    except (AttributeError, ValueError):
        return default


def _identity_value(value: str) -> tuple[int | None, str]:
    if not value:
        return None, ""
    head, separator, comm = value.partition(":")
    try:
        number = int(head.strip())
    except ValueError:
        return None, ""
    return number, comm.strip() if separator else ""


def _split_cells(line: str, field_separator: str | None) -> list[str]:
    separator = field_separator or ("\t" if "\t" in line else None)
    if separator and separator in line:
        return [cell.strip() for cell in line.split(separator)]
    return [cell.strip() for cell in _ROW_SPLIT.split(line)]


def event_matches(event: str, expected: set[str]) -> bool:
    def key(value: str) -> str:
        value = value.replace(" ", "").lower()
        value = value.split(",", 1)[0]
        parts = value.split("/")
        if len(parts) >= 2:
            name = parts[1].split("=", 1)[0]
            return parts[0] if name in ("", "period", "freq") else parts[0] + "/" + name
        return parts[0]

    value = key(event)
    return any(value == key(item) or value.startswith(key(item) + ":") for item in expected)


@dataclass
class MemSymbol:
    symbol: str
    dso: str
    samples: int = 0
    weight: int = 0          # summed access latency, cycles (stall-time proxy)
    dram_samples: int = 0


@dataclass
class MemoryProfile:
    total_samples: int = 0                 # all IBS samples seen
    classified_samples: int = 0            # those with an access level
    level_samples: dict[str, int] = field(default_factory=dict)
    level_weight: dict[str, int] = field(default_factory=dict)
    tlb_samples: dict[str, int] = field(default_factory=dict)
    bands: dict[str, int] = field(default_factory=dict)   # band -> samples
    by_symbol: dict[str, MemSymbol] = field(default_factory=dict)
    tid: int | None = None
    tgid: int | None = None
    comm: str = ""
    by_tid: dict[int, "MemoryProfile"] = field(default_factory=dict, repr=False)

    @property
    def avg_latency(self) -> float | None:
        if not self.classified_samples:
            return None
        w = sum(self.level_weight.values())
        return w / self.classified_samples

    def level_pct(self, level: str) -> float | None:
        if not self.classified_samples:
            return None
        return self.level_samples.get(level, 0) / self.classified_samples * 100.0

    def top_symbols(self, n: int = 12) -> list[MemSymbol]:
        return sorted(self.by_symbol.values(),
                      key=lambda s: s.weight, reverse=True)[:n]

    def total_weight(self) -> int:
        return sum(self.level_weight.values())


def _add_row(prof: MemoryProfile, samples: int, weight: int, level: str,
             symbol: str, dso: str, tlb: str, weight_is_average: bool = False) -> None:
    prof.total_samples += samples
    if level != "unclassified":
        total_weight = weight * samples if weight_is_average else weight
        prof.classified_samples += samples
        prof.level_samples[level] = prof.level_samples.get(level, 0) + samples
        prof.level_weight[level] = prof.level_weight.get(level, 0) + total_weight

        avg = weight if weight_is_average else weight / samples if samples else 0
        for name, lo, hi in LATENCY_BANDS:
            if lo <= avg < hi or (hi == float("inf") and avg >= lo):
                prof.bands[name] = prof.bands.get(name, 0) + samples
                break

    tl = tlb if tlb and tlb != "N/A" else "n/a"
    prof.tlb_samples[tl] = prof.tlb_samples.get(tl, 0) + samples

    ms = prof.by_symbol.get(symbol)
    if ms is None:
        ms = prof.by_symbol[symbol] = MemSymbol(symbol=symbol, dso=dso)
    ms.samples += samples
    if level != "unclassified":
        ms.weight += weight * samples if weight_is_average else weight
    if level == "DRAM":
        ms.dram_samples += samples


def parse_mem_report(text: str, memory_events: set[str] | None = None,
                     field_separator: str | None = None,
                     weight_is_average: bool | None = False) -> MemoryProfile:
    prof = MemoryProfile()
    expected_events = {str(event) for event in (memory_events or set())}
    col: dict[str, int] = {}
    section_allowed = True
    section_weight_is_average = bool(weight_is_average)

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        low = stripped.lower()

        event_match = _EVENT_SAMPLES.search(stripped)
        if event_match:
            section_allowed = not expected_events or event_matches(event_match.group(1), expected_events)
            col = {}
            continue

        if not col:
            hits = sum(1 for k in _HEADER_HINTS if k in low)
            if hits >= 4 and "overhead" in low:
                cells = _split_cells(stripped.lstrip("# "), field_separator)
                for i, c in enumerate(cells):
                    col[c.lower()] = i
                if weight_is_average is None:
                    section_weight_is_average = "pid:command" in col or "tgid:command" in col
            continue

        if not section_allowed:
            continue

        if any(low.startswith(p.lower()) for p in _SKIP_PREFIXES):
            continue

        cells = _split_cells(stripped, field_separator)

        def at(name: str) -> str:
            idx = col.get(name.lower())
            return cells[idx] if idx is not None and idx < len(cells) else ""

        samples = _int_value(at("Samples"))
        if samples <= 0:
            continue
        weight = 0
        total_period = at("Period")
        if total_period not in ("", "N/A"):
            weight = _int_value(total_period)
        else:
            local_weight = at("Local Weight")
            if local_weight not in ("", "N/A"):
                weight = _int_value(local_weight)
        row_weight_is_average = section_weight_is_average and total_period in ("", "N/A")

        access = at("Memory access")
        level = _classify_access(access)
        symbol = _clean_symbol(at("Symbol"))
        dso = at("Shared Object") or "[unknown]"
        tlb = at("TLB access") or "N/A"

        tgid, tgid_comm = _identity_value(at("Tgid:Command"))
        tid, tid_comm = _identity_value(at("Pid:Command"))
        if tid is None:
            tid, tid_comm = _identity_value(at("Tid:Command"))
        comm = tid_comm or at("Command") or tgid_comm

        _add_row(prof, samples, weight, level, symbol, dso, tlb, row_weight_is_average)
        if tid is not None:
            thread = prof.by_tid.get(tid)
            if thread is None:
                thread = MemoryProfile(tid=tid, tgid=tgid, comm=comm)
                prof.by_tid[tid] = thread
            else:
                if thread.tgid is None and tgid is not None:
                    thread.tgid = tgid
                if not thread.comm and comm:
                    thread.comm = comm
            _add_row(thread, samples, weight, level, symbol, dso, tlb, row_weight_is_average)

    return prof


__all__ = ["MemoryProfile", "MemSymbol", "event_matches", "parse_mem_report", "LATENCY_BANDS"]

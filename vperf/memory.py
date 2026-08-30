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

_SKIP_PREFIXES = (
    "#", "Warning:", "Kernel address", "Check ", "As no ",
    "Samples in kernel", "can't be resolved",
)

_HEADER_HINTS = {
    "overhead": None,
    "samples": None,
    "local weight": None,
    "memory access": None,
    "symbol": None,
    "shared object": None,
    "data object": None,
    "tlb access": None,
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


def parse_mem_report(text: str) -> MemoryProfile:
    prof = MemoryProfile()

    col: dict[str, int] = {}
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        low = stripped.lower()

        # locate the header row once (perf prefixes it with '#')
        if not col:
            hits = sum(1 for k in _HEADER_HINTS if k in low)
            if hits >= 4 and "overhead" in low:
                cells = [c.strip() for c in _ROW_SPLIT.split(stripped.lstrip("# "))]
                for i, c in enumerate(cells):
                    col[c.lower()] = i
            continue

        if any(low.startswith(p.lower()) for p in _SKIP_PREFIXES):
            continue

        cells = [c.strip() for c in _ROW_SPLIT.split(stripped)]

        def at(name: str) -> str:
            idx = col.get(name.lower())
            return cells[idx] if idx is not None and idx < len(cells) else ""

        try:
            samples = int(at("Samples") or 0)
        except ValueError:
            samples = 0
        if samples <= 0:
            continue
        try:
            weight = int(float(at("Local Weight"))) if at("Local Weight") not in ("", "N/A") else 0
        except ValueError:
            weight = 0

        access = at("Memory access")
        level = _classify_access(access)
        symbol = _clean_symbol(at("Symbol"))
        dso = at("Shared Object") or "[unknown]"
        tlb = at("TLB access") or "N/A"

        prof.total_samples += samples
        if level != "unclassified":
            prof.classified_samples += samples
            prof.level_samples[level] = prof.level_samples.get(level, 0) + samples
            prof.level_weight[level] = prof.level_weight.get(level, 0) + weight

            avg = weight / samples if samples else 0
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
        ms.weight += weight if level != "unclassified" else 0
        if level == "DRAM":
            ms.dram_samples += samples

    return prof


__all__ = ["MemoryProfile", "MemSymbol", "parse_mem_report", "LATENCY_BANDS"]

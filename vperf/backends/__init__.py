"""Per-platform profiling backends.

vperf collects on Linux through ``perf`` (see ``vperf/collector.py``) and on
macOS through the ``sample`` command plus ``ps`` accounting (see
``vperf/backends/macos.py``).  ``collector.collect`` dispatches by platform.
"""

"""Process-level performance sampler (V10 Phase 11).

Pure helpers for the ``perf_smoke.py`` harness:

- :func:`sample_once` returns the current ``(rss_bytes, cpu_percent)``
  for a pid without shelling out to ``ps`` every tick.
- :func:`evaluate_budgets` compares a recorded trace against the
  plan's *hard budgets* and returns a structured report the CLI can
  pretty-print or assert on.

Kept testable: everything here is a pure function of the input
samples. The subprocess orchestration lives in the ``scripts/``
entry point.

Budgets (from V10 plan Section 3.1):

- Swift App RSS       < 40 MB
- Python Agent RSS    < 150 MB
- CPU idle            < 1 %   (60s sample)
- CPU hot             < 15 %  (under workload)
- Cold start          < 1.5 s (agent_ready marker)
- Wake to interactive < 0.5 s (NSWorkspace.didWakeNotification)
- LLM first token     < 2 s   (Python-side stream stamp)
- Frame drop          0 %     (Swift CADisplayLink)
- IPC p99 round trip  < 10 ms

The evaluator accepts each metric as an optional kwarg; the
caller — ``perf_smoke.py`` for the Python-side budgets, the
forthcoming Swift harness for the AppKit-side ones — supplies
whichever it has actually measured. Anything left ``None`` is
silently absent from the report rather than reported as failed,
so a partial run never falsely fails the build.
"""

from __future__ import annotations

import os
import resource
import time
from dataclasses import dataclass, field
from statistics import mean

PYTHON_RSS_HARD_BUDGET_BYTES = 150 * 1024 * 1024
SWIFT_RSS_HARD_BUDGET_BYTES = 40 * 1024 * 1024
CPU_IDLE_HARD_BUDGET_PCT = 1.0
# V10 §3.1 row 4: under load the agent must stay below 15 %
# averaged over the workload window.
CPU_HOT_HARD_BUDGET_PCT = 15.0
COLD_START_HARD_BUDGET_S = 1.5
# V10 §3.1 row 6: from ``NSWorkspace.didWakeNotification`` to the
# first interactive frame the Swift shell renders.
WAKE_HARD_BUDGET_S = 0.5
# V10 §3.1 row 7: time from chat user-message accepted to first
# streamed LLM token — measured Python-side around the LLM call.
LLM_FIRST_TOKEN_HARD_BUDGET_S = 2.0
# V10 §3.1 row 8: ``CADisplayLink`` reported drop ratio; the plan's
# target is "zero" so any non-zero ratio fails the budget.
FRAME_DROP_HARD_BUDGET_PCT = 0.0
IPC_P99_HARD_BUDGET_MS = 10.0


@dataclass(frozen=True)
class ProcessSample:
    """One snapshot of a running process's resource usage."""

    ts_ms: int
    rss_bytes: int
    cpu_percent: float

    @property
    def rss_mb(self) -> float:
        return self.rss_bytes / (1024 * 1024)


@dataclass
class _PsStatCache:
    """Keeps the previous ``(cpu_ticks, wall_ms)`` reading per pid so
    the next CPU % can be computed relative to it."""

    prev_cpu_seconds: float | None = None
    prev_wall_seconds: float | None = None


_PS_CACHE: dict[int, _PsStatCache] = {}


def sample_once(pid: int, *, now_ms: int | None = None) -> ProcessSample:
    """Return one :class:`ProcessSample` for ``pid``.

    CPU % is computed against the previous sample for the same pid
    so the first call always returns ``0.0`` (no history yet).
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    rss, cpu_seconds = _read_proc_stats(pid)
    cache = _PS_CACHE.setdefault(pid, _PsStatCache())
    wall_s = now_ms / 1000.0
    cpu_pct = 0.0
    if (
        cache.prev_cpu_seconds is not None
        and cache.prev_wall_seconds is not None
    ):
        d_cpu = cpu_seconds - cache.prev_cpu_seconds
        d_wall = wall_s - cache.prev_wall_seconds
        if d_wall > 0:
            cpu_pct = max(0.0, 100.0 * d_cpu / d_wall)
    cache.prev_cpu_seconds = cpu_seconds
    cache.prev_wall_seconds = wall_s
    return ProcessSample(ts_ms=now_ms, rss_bytes=rss, cpu_percent=cpu_pct)


def reset_sampler_cache() -> None:
    """Forget every pid's prior reading. Tests call this to get
    reproducible first-sample behaviour."""
    _PS_CACHE.clear()


def _read_proc_stats(pid: int) -> tuple[int, float]:
    """macOS/Linux-portable ``(rss_bytes, cpu_seconds)`` reader.

    macOS doesn't expose ``/proc``, so we fall back to ``ps -o rss,time``
    which is slower but universally available. Linux uses
    ``/proc/<pid>/stat`` for a ~1 µs read.
    """
    stat_path = f"/proc/{pid}/stat"
    if os.path.exists(stat_path):
        with open(stat_path, encoding="utf-8") as fh:
            fields = fh.read().split()
        # Field 22 (``rss``) is in pages; multiply by page size.
        utime = float(fields[13])
        stime = float(fields[14])
        rss_pages = int(fields[23])
        page_size = resource.getpagesize()
        hz = os.sysconf("SC_CLK_TCK")
        return rss_pages * page_size, (utime + stime) / hz
    # macOS path — use ``ps``.
    import subprocess

    out = subprocess.check_output(
        ["ps", "-p", str(pid), "-o", "rss=,time="],
        text=True,
    ).strip().split()
    if len(out) < 2:
        raise RuntimeError(f"ps returned no data for pid {pid}")
    rss_kb = int(out[0])
    cpu_seconds = _parse_cpu_time(out[1])
    return rss_kb * 1024, cpu_seconds


def _parse_cpu_time(raw: str) -> float:
    """Parse ``ps``'s cumulative CPU field (``MMM:SS.cc`` or
    ``HH:MM:SS``) into seconds."""
    parts = raw.split(":")
    if len(parts) == 2:
        minutes = int(parts[0])
        seconds = float(parts[1])
        return minutes * 60 + seconds
    if len(parts) == 3:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])
        return hours * 3600 + minutes * 60 + seconds
    return float(raw)


@dataclass
class BudgetEvaluation:
    """Report of one budget check: pass / fail / n/a."""

    label: str
    value: float
    budget: float
    unit: str
    ok: bool
    detail: str = ""


@dataclass
class PerfReport:
    """Aggregated outcome of a perf smoke run."""

    samples: list[ProcessSample] = field(default_factory=list)
    cold_start_s: float | None = None
    ipc_round_trips_ms: list[float] = field(default_factory=list)
    hot_cpu_pct: float | None = None
    wake_s: float | None = None
    llm_first_token_s: float | None = None
    frame_drop_pct: float | None = None
    process_label: str = "python"
    evaluations: list[BudgetEvaluation] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return all(e.ok for e in self.evaluations)


def evaluate_budgets(
    samples: list[ProcessSample],
    *,
    process_label: str = "python",
    cold_start_s: float | None = None,
    ipc_round_trips_ms: list[float] | None = None,
    hot_cpu_pct: float | None = None,
    wake_s: float | None = None,
    llm_first_token_s: float | None = None,
    frame_drop_pct: float | None = None,
) -> PerfReport:
    """Score the supplied measurements against the V10 hard budgets.

    Zero samples is treated as "nothing to say" for CPU/RSS rather
    than a failure — the harness either ran too short or the process
    exited early, and we surface that as missing evaluation rather
    than a false-green pass. The same applies to every optional
    kwarg: a ``None`` means "not measured this run" and skips the
    corresponding evaluation, so partial harnesses (Python-only,
    Swift-only, no-LLM) never falsely fail.
    """
    evaluations: list[BudgetEvaluation] = []
    if samples:
        max_rss = max(s.rss_bytes for s in samples)
        rss_budget = (
            SWIFT_RSS_HARD_BUDGET_BYTES
            if process_label == "swift"
            else PYTHON_RSS_HARD_BUDGET_BYTES
        )
        evaluations.append(
            BudgetEvaluation(
                label=f"{process_label} RSS peak",
                value=max_rss / (1024 * 1024),
                budget=rss_budget / (1024 * 1024),
                unit="MB",
                ok=max_rss < rss_budget,
                detail=f"peak over {len(samples)} samples",
            )
        )
        cpu_samples = [s.cpu_percent for s in samples]
        # Skip the zero first-sample (no prior reading) from the mean
        # so we don't artificially smooth the CPU budget.
        tail = cpu_samples[1:] if len(cpu_samples) > 1 else cpu_samples
        avg_cpu = mean(tail) if tail else 0.0
        evaluations.append(
            BudgetEvaluation(
                label=f"{process_label} CPU (mean)",
                value=avg_cpu,
                budget=CPU_IDLE_HARD_BUDGET_PCT,
                unit="%",
                ok=avg_cpu < CPU_IDLE_HARD_BUDGET_PCT,
                detail=f"mean of {len(tail)} samples",
            )
        )
    if cold_start_s is not None:
        evaluations.append(
            BudgetEvaluation(
                label="cold start",
                value=cold_start_s,
                budget=COLD_START_HARD_BUDGET_S,
                unit="s",
                ok=cold_start_s < COLD_START_HARD_BUDGET_S,
            )
        )
    if hot_cpu_pct is not None:
        evaluations.append(
            BudgetEvaluation(
                label="CPU hot",
                value=hot_cpu_pct,
                budget=CPU_HOT_HARD_BUDGET_PCT,
                unit="%",
                ok=hot_cpu_pct < CPU_HOT_HARD_BUDGET_PCT,
                detail="under workload",
            )
        )
    if wake_s is not None:
        evaluations.append(
            BudgetEvaluation(
                label="wake to interactive",
                value=wake_s,
                budget=WAKE_HARD_BUDGET_S,
                unit="s",
                ok=wake_s < WAKE_HARD_BUDGET_S,
            )
        )
    if llm_first_token_s is not None:
        evaluations.append(
            BudgetEvaluation(
                label="LLM first token",
                value=llm_first_token_s,
                budget=LLM_FIRST_TOKEN_HARD_BUDGET_S,
                unit="s",
                ok=llm_first_token_s < LLM_FIRST_TOKEN_HARD_BUDGET_S,
            )
        )
    if frame_drop_pct is not None:
        evaluations.append(
            BudgetEvaluation(
                label="frame drop",
                value=frame_drop_pct,
                budget=FRAME_DROP_HARD_BUDGET_PCT,
                unit="%",
                # Plan target is "0%" — any positive drop fails. We
                # use ``<=`` so a measured ``0.0`` passes (the
                # CADisplayLink rounding is enforced in the Swift
                # harness, not here).
                ok=frame_drop_pct <= FRAME_DROP_HARD_BUDGET_PCT,
            )
        )
    if ipc_round_trips_ms:
        rtts = sorted(ipc_round_trips_ms)
        # P99 — simple nearest-rank interpolation; for n < 100 the
        # plain ``rtts[int(len(rtts)*0.99)]`` is an upper-bound proxy.
        idx = min(len(rtts) - 1, int(len(rtts) * 0.99))
        p99 = rtts[idx]
        evaluations.append(
            BudgetEvaluation(
                label="IPC p99",
                value=p99,
                budget=IPC_P99_HARD_BUDGET_MS,
                unit="ms",
                ok=p99 < IPC_P99_HARD_BUDGET_MS,
                detail=f"n={len(rtts)}",
            )
        )
    return PerfReport(
        samples=list(samples),
        cold_start_s=cold_start_s,
        ipc_round_trips_ms=list(ipc_round_trips_ms or []),
        hot_cpu_pct=hot_cpu_pct,
        wake_s=wake_s,
        llm_first_token_s=llm_first_token_s,
        frame_drop_pct=frame_drop_pct,
        process_label=process_label,
        evaluations=evaluations,
    )


def format_report(report: PerfReport) -> str:
    """Multi-line human summary suitable for shell output."""
    if not report.evaluations:
        return "[perf] no evaluations — sampler produced no data."
    lines: list[str] = []
    width = max(len(e.label) for e in report.evaluations)
    for e in report.evaluations:
        flag = "✅" if e.ok else "❌"
        detail = f"  ({e.detail})" if e.detail else ""
        lines.append(
            f"  {flag}  {e.label.ljust(width)}  "
            f"{e.value:.2f}{e.unit}  <  {e.budget:.2f}{e.unit}{detail}"
        )
    lines.append(
        "\nVerdict: "
        + ("✅ all budgets within limits" if report.all_ok else "❌ some budgets exceeded")
    )
    return "\n".join(lines)


__all__ = [
    "BudgetEvaluation",
    "COLD_START_HARD_BUDGET_S",
    "CPU_HOT_HARD_BUDGET_PCT",
    "CPU_IDLE_HARD_BUDGET_PCT",
    "FRAME_DROP_HARD_BUDGET_PCT",
    "IPC_P99_HARD_BUDGET_MS",
    "LLM_FIRST_TOKEN_HARD_BUDGET_S",
    "PYTHON_RSS_HARD_BUDGET_BYTES",
    "PerfReport",
    "ProcessSample",
    "SWIFT_RSS_HARD_BUDGET_BYTES",
    "WAKE_HARD_BUDGET_S",
    "evaluate_budgets",
    "format_report",
    "reset_sampler_cache",
    "sample_once",
]

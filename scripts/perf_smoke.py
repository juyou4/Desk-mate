#!/usr/bin/env python3
"""Real perf-smoke harness (V10 Phase 11).

Starts the ``deskmate_agent`` module as a subprocess, waits for the
``agent_ready`` structlog marker, then samples its RSS + CPU for a
configurable window and (optionally) measures the IPC ping/pong
round-trip latency. After teardown we compare each metric against
the V10 hard budgets (see :mod:`deskmate_agent.perf`) and exit
non-zero if any fail.

Usage::

    scripts/perf_smoke.py                   # 30s default sample window
    scripts/perf_smoke.py --duration-s 60
    scripts/perf_smoke.py --interval-s 2.0
    scripts/perf_smoke.py --output-json report.json
    scripts/perf_smoke.py --ipc-pings 0     # skip the IPC measurement

The script never calls Instruments — that's a manual Xcode workflow.
It instead gives contributors + CI a quick pass/fail on RSS + CPU
+ IPC p99 so regressions surface early.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / "agent"
sys.path.insert(0, str(AGENT_DIR))

# noqa: E402 — these imports must follow the sys.path mutation above.
from deskmate_agent.bridge import (  # noqa: E402
    LineBuffer,
    decode_envelope,
    encode_envelope,
)
from deskmate_agent.perf import (  # noqa: E402
    ProcessSample,
    evaluate_budgets,
    format_report,
    reset_sampler_cache,
    sample_once,
)
from deskmate_agent.protocol.envelope import (  # noqa: E402
    BridgeEnvelope,
    EnvelopeType,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="perf_smoke", description=__doc__)
    p.add_argument(
        "--duration-s",
        type=float,
        default=30.0,
        help="post-ready sampling duration (default 30s)",
    )
    p.add_argument(
        "--interval-s",
        type=float,
        default=1.0,
        help="sampling interval (default 1s)",
    )
    p.add_argument(
        "--startup-timeout-s",
        type=float,
        default=10.0,
        help="max wait for agent_ready marker (default 10s)",
    )
    p.add_argument(
        "--output-json",
        default=None,
        help="also write a machine-readable report to this path",
    )
    p.add_argument(
        "--ipc-pings",
        type=int,
        default=100,
        help=(
            "number of ping/pong round trips to measure for the "
            "IPC p99 budget (default 100; set 0 to skip)"
        ),
    )
    p.add_argument(
        "--ipc-ping-timeout-s",
        type=float,
        default=2.0,
        help="per-ping reply timeout (default 2s)",
    )
    p.add_argument(
        "--workload",
        action="store_true",
        help=(
            "after the idle sample window, drive the agent with "
            "high-frequency PERCEPTION envelopes and capture hot-state "
            "CPU for the §3.1 row 4 budget (15%% under load)"
        ),
    )
    p.add_argument(
        "--workload-duration-s",
        type=float,
        default=10.0,
        help="hot workload duration (default 10s; only with --workload)",
    )
    p.add_argument(
        "--workload-perception-hz",
        type=float,
        default=20.0,
        help=(
            "PERCEPTION envelopes per second during the hot workload "
            "(default 20 ≈ realistic Swift sampler peak)"
        ),
    )
    p.add_argument(
        "--workload-cpu-interval-s",
        type=float,
        default=0.5,
        help="CPU sample interval during the hot workload (default 0.5s)",
    )
    return p.parse_args()


def _wait_for_ready(
    proc: subprocess.Popen,
    *,
    timeout_s: float,
) -> float:
    """Block until the agent prints the ``deskmate_agent.started`` log
    line (via structlog) or ``timeout_s`` elapses. Returns the
    elapsed cold-start seconds.

    structlog + our default config writes JSON events to **stdout**,
    one per line. The cold-start marker is ``deskmate_agent.started``.
    """
    deadline = time.time() + timeout_s
    start = time.time()
    stream = proc.stdout
    while time.time() < deadline:
        if proc.poll() is not None:
            stderr = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(
                f"agent exited with code {proc.returncode} before ready: "
                f"{stderr[:400]}"
            )
        line = stream.readline() if stream else ""
        if not line:
            time.sleep(0.05)
            continue
        if "deskmate_agent.started" in line:
            return time.time() - start
    raise TimeoutError(
        f"agent did not emit ready marker within {timeout_s}s"
    )


def _sample_loop(
    pid: int,
    *,
    duration_s: float,
    interval_s: float,
) -> list[ProcessSample]:
    samples: list[ProcessSample] = []
    reset_sampler_cache()
    # First sample has no history → cpu_pct=0; we keep it for RSS.
    samples.append(sample_once(pid))
    deadline = time.time() + duration_s
    while time.time() < deadline:
        time.sleep(interval_s)
        try:
            samples.append(sample_once(pid))
        except Exception as exc:  # noqa: BLE001
            print(f"[perf] sample failed: {exc}", file=sys.stderr)
            break
    return samples


# ---------------------------------------------------------------------------
# IPC ping/pong measurement (V10 §3.1 row 9)
# ---------------------------------------------------------------------------


async def _measure_ipc_round_trips(
    socket_path: Path,
    *,
    count: int,
    per_ping_timeout_s: float,
) -> list[float]:
    """Connect to the running agent's UDS, exchange ``count`` ping/pong
    envelopes back-to-back, and return the per-round-trip latency in
    milliseconds.

    Non-PONG envelopes (e.g. ``state.snapshot`` pushed on connect, the
    occasional 30-second heartbeat ping, intent traffic) are silently
    skipped so the measurement focuses on bridge round-trip time
    rather than business processing.
    """
    if count <= 0:
        return []
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
    except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
        print(f"[perf] IPC connect failed: {exc}", file=sys.stderr)
        return []

    pending: deque[bytes] = deque()
    buf = LineBuffer()
    rtts: list[float] = []
    # Server pushes a ``state.snapshot`` on connect (Phase 1d). Drain
    # whatever lands in the first 100 ms so the very first PING isn't
    # stuck behind a snapshot decode + handler chain — that's
    # business processing time, not bridge round-trip time.
    drain_deadline = asyncio.get_running_loop().time() + 0.1
    while asyncio.get_running_loop().time() < drain_deadline:
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=0.05)
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        for line in buf.feed(chunk):
            pending.append(line)  # consumed-and-ignored below
    pending.clear()
    try:
        for i in range(count):
            sent_ns = time.perf_counter_ns()
            writer.write(encode_envelope(BridgeEnvelope.of(EnvelopeType.PING)))
            try:
                await writer.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                print(f"[perf] IPC drain failed at i={i}: {exc}", file=sys.stderr)
                break
            try:
                await asyncio.wait_for(
                    _await_pong(reader, pending, buf),
                    timeout=per_ping_timeout_s,
                )
            except asyncio.TimeoutError:
                print(
                    f"[perf] IPC pong timeout at i={i}; "
                    f"collected {len(rtts)}/{count}",
                    file=sys.stderr,
                )
                break
            except EOFError:
                print(
                    f"[perf] IPC stream closed at i={i}; "
                    f"collected {len(rtts)}/{count}",
                    file=sys.stderr,
                )
                break
            recv_ns = time.perf_counter_ns()
            rtts.append((recv_ns - sent_ns) / 1e6)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
    return rtts


async def _await_pong(
    reader: asyncio.StreamReader,
    pending: deque[bytes],
    buf: LineBuffer,
) -> None:
    """Read until the next ``EnvelopeType.PONG`` envelope arrives.

    Discards any envelope of any other type (state snapshots, intents,
    heartbeat pings the server may emit) so a measurement run is
    robust against background traffic.
    """
    while True:
        while pending:
            line = pending.popleft()
            try:
                env = decode_envelope(line)
            except Exception:  # noqa: BLE001
                # Malformed lines are skipped — they're a server-side
                # bug and shouldn't break the harness.
                continue
            if env.type is EnvelopeType.PONG:
                return
        chunk = await reader.read(4096)
        if not chunk:
            raise EOFError("bridge stream closed")
        for line in buf.feed(chunk):
            pending.append(line)


# ---------------------------------------------------------------------------
# Hot workload (V10 §3.1 row 4)
# ---------------------------------------------------------------------------


async def _run_hot_workload(
    socket_path: Path,
    *,
    pid: int,
    duration_s: float,
    perception_hz: float,
    cpu_sample_interval_s: float,
) -> list[ProcessSample]:
    """Drive the agent with PERCEPTION envelopes at ``perception_hz``
    for ``duration_s`` while sampling its CPU at
    ``cpu_sample_interval_s``. Returns the hot-state samples so the
    caller can feed the mean into ``evaluate_budgets(hot_cpu_pct=…)``.

    The PERCEPTION payload deliberately *varies* the ``app`` field
    every tick so :class:`PerceptionDeduper` accepts most of them —
    otherwise we'd just be measuring the dedupe short-circuit, not
    the actual proactive chain CPU profile.
    """
    if duration_s <= 0 or perception_hz <= 0:
        return []
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
    except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
        print(f"[perf] workload connect failed: {exc}", file=sys.stderr)
        return []

    stop = asyncio.Event()
    samples: list[ProcessSample] = []

    async def _drainer() -> None:
        """Swallow whatever the server pushes (state snapshot, intents
        emitted in response to perception, heartbeat) so the kernel
        socket buffer never fills up and back-pressures our writes."""
        try:
            while not stop.is_set():
                try:
                    chunk = await asyncio.wait_for(reader.read(4096), timeout=0.05)
                except asyncio.TimeoutError:
                    continue
                if not chunk:
                    return
        except (asyncio.CancelledError, ConnectionResetError):
            return

    async def _sender() -> None:
        period_s = 1.0 / perception_hz
        i = 0
        while not stop.is_set():
            try:
                writer.write(
                    encode_envelope(
                        BridgeEnvelope.of(
                            EnvelopeType.PERCEPTION,
                            {
                                "user_state": "active",
                                "focus": "casual",
                                "app": f"com.deskmate.workload.{i % 32}",
                                "idle_ms": (i * 137) % 5000,
                            },
                        )
                    )
                )
                await writer.drain()
            except (BrokenPipeError, ConnectionResetError):
                return
            try:
                await asyncio.sleep(period_s)
            except asyncio.CancelledError:
                return
            i += 1

    drainer_task = asyncio.create_task(_drainer())
    sender_task = asyncio.create_task(_sender())
    try:
        # Reset psutil's per-process counter so the first hot sample
        # represents the workload, not the idle window we just left.
        reset_sampler_cache()
        sample_once(pid)  # priming sample (cpu_pct=0; discarded)
        deadline = asyncio.get_running_loop().time() + duration_s
        while asyncio.get_running_loop().time() < deadline:
            try:
                await asyncio.sleep(cpu_sample_interval_s)
            except asyncio.CancelledError:
                break
            try:
                samples.append(sample_once(pid))
            except Exception as exc:  # noqa: BLE001
                print(f"[perf] hot sample failed: {exc}", file=sys.stderr)
                break
    finally:
        stop.set()
        sender_task.cancel()
        drainer_task.cancel()
        await asyncio.gather(
            sender_task, drainer_task, return_exceptions=True
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
    return samples


def _hot_cpu_mean_pct(samples: list[ProcessSample]) -> float | None:
    """Return the mean CPU % across hot-state samples, or ``None`` if
    we have nothing to average. The first sample in a fresh run
    always reports 0% (no history) and is excluded so it can't pull
    the mean toward zero on short workloads."""
    contributing = [s.cpu_percent for s in samples if s.cpu_percent > 0]
    if not contributing:
        return None
    return sum(contributing) / len(contributing)


def main() -> int:
    args = _parse_args()
    socket_dir = Path(tempfile.mkdtemp(prefix="deskmate-perf-"))
    socket_path = socket_dir / "ipc.sock"
    db_dir = socket_dir / "db"
    env = os.environ.copy()
    # Disable LLM prewarm so we measure the pure runtime, not the
    # model probe. A real LLM-enabled run is a separate concern.
    env.pop("DESKMATE_LLM_API_KEY", None)

    # Override default socket / db paths so we don't clobber a
    # running agent's state.
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "deskmate_agent",
    ]
    print(f"[perf] launching agent with cwd={AGENT_DIR}")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=AGENT_DIR,
        env={
            **env,
            "DESKMATE_SOCKET_PATH": str(socket_path),
            "DESKMATE_DB_DIR": str(db_dir),
        },
        text=True,
        bufsize=1,
    )

    rtts: list[float] = []
    hot_samples: list[ProcessSample] = []
    hot_cpu_pct: float | None = None
    try:
        print(f"[perf] waiting for agent_ready (pid={proc.pid})")
        cold_start_s = _wait_for_ready(
            proc, timeout_s=args.startup_timeout_s
        )
        print(f"[perf] ready in {cold_start_s:.2f}s — sampling "
              f"{args.duration_s}s every {args.interval_s}s")
        samples = _sample_loop(
            proc.pid,
            duration_s=args.duration_s,
            interval_s=args.interval_s,
        )
        # V10 §3.1 row 4: drive the agent with high-frequency
        # PERCEPTION envelopes and capture hot-state CPU. Done
        # *before* the IPC measurement so the bridge stays single-
        # client (server requires it); a brief settle sleep lets
        # the server tear down the workload connection cleanly
        # before we reconnect for ping/pong.
        if args.workload:
            print(
                f"[perf] hot workload {args.workload_duration_s:.1f}s "
                f"@ {args.workload_perception_hz:.0f} Hz PERCEPTION"
            )
            try:
                hot_samples = asyncio.run(
                    _run_hot_workload(
                        socket_path,
                        pid=proc.pid,
                        duration_s=args.workload_duration_s,
                        perception_hz=args.workload_perception_hz,
                        cpu_sample_interval_s=args.workload_cpu_interval_s,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[perf] workload failed: {exc}", file=sys.stderr)
                hot_samples = []
            hot_cpu_pct = _hot_cpu_mean_pct(hot_samples)
            if hot_cpu_pct is None:
                print("[perf] hot CPU mean = (no samples)")
            else:
                print(
                    f"[perf] hot CPU mean = {hot_cpu_pct:.2f}% "
                    f"(n={sum(1 for s in hot_samples if s.cpu_percent > 0)})"
                )
            time.sleep(0.05)  # let server's _handle_client unwind
        # V10 §3.1 row 9: measure IPC ping/pong p99 round-trip after
        # the sampler has stopped poking the process so neither
        # measurement biases the other. Done before SIGTERM so the
        # bridge socket is still alive; failures fall through and
        # leave ``rtts == []`` which evaluate_budgets treats as
        # "not measured" rather than a false-fail.
        if args.ipc_pings > 0:
            print(f"[perf] measuring IPC p99 over {args.ipc_pings} pings")
            try:
                rtts = asyncio.run(
                    _measure_ipc_round_trips(
                        socket_path,
                        count=args.ipc_pings,
                        per_ping_timeout_s=args.ipc_ping_timeout_s,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[perf] IPC measurement failed: {exc}", file=sys.stderr)
                rtts = []
            print(f"[perf] collected {len(rtts)}/{args.ipc_pings} IPC round trips")
    finally:
        print("[perf] shutting down agent")
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    report = evaluate_budgets(
        samples,
        process_label="python",
        cold_start_s=cold_start_s,
        ipc_round_trips_ms=rtts or None,
        hot_cpu_pct=hot_cpu_pct,
    )
    print(format_report(report))

    if args.output_json:
        _write_json_report(report, Path(args.output_json))

    return 0 if report.all_ok else 1


def _write_json_report(report, path: Path) -> None:
    payload = {
        "samples": [
            {
                "ts_ms": s.ts_ms,
                "rss_bytes": s.rss_bytes,
                "cpu_percent": s.cpu_percent,
            }
            for s in report.samples
        ],
        "cold_start_s": report.cold_start_s,
        "ipc_round_trips_ms": list(report.ipc_round_trips_ms),
        "hot_cpu_pct": report.hot_cpu_pct,
        "process_label": report.process_label,
        "evaluations": [
            {
                "label": e.label,
                "value": e.value,
                "budget": e.budget,
                "unit": e.unit,
                "ok": e.ok,
                "detail": e.detail,
            }
            for e in report.evaluations
        ],
        "all_ok": report.all_ok,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"[perf] wrote JSON report to {path}")


if __name__ == "__main__":
    raise SystemExit(main())

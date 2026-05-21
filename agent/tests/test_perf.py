"""Perf sampler + budget evaluator tests (V10 Phase 11)."""

from __future__ import annotations

from deskmate_agent.perf import (
    COLD_START_HARD_BUDGET_S,
    CPU_HOT_HARD_BUDGET_PCT,
    CPU_IDLE_HARD_BUDGET_PCT,
    FRAME_DROP_HARD_BUDGET_PCT,
    IPC_P99_HARD_BUDGET_MS,
    LLM_FIRST_TOKEN_HARD_BUDGET_S,
    PYTHON_RSS_HARD_BUDGET_BYTES,
    SWIFT_RSS_HARD_BUDGET_BYTES,
    WAKE_HARD_BUDGET_S,
    ProcessSample,
    _parse_cpu_time,
    evaluate_budgets,
    format_report,
)


def _samples(
    *,
    rss_mb: float,
    cpu_pcts: list[float],
) -> list[ProcessSample]:
    rss_bytes = int(rss_mb * 1024 * 1024)
    return [
        ProcessSample(ts_ms=i * 1000, rss_bytes=rss_bytes, cpu_percent=p)
        for i, p in enumerate(cpu_pcts)
    ]


# ---------------------------------------------------------------------------
# Budget evaluator
# ---------------------------------------------------------------------------


def test_python_rss_under_budget_passes() -> None:
    report = evaluate_budgets(_samples(rss_mb=100, cpu_pcts=[0, 0.5, 0.4]))
    rss_eval = next(e for e in report.evaluations if "RSS" in e.label)
    assert rss_eval.ok is True
    assert rss_eval.value == 100.0


def test_python_rss_over_budget_fails() -> None:
    report = evaluate_budgets(_samples(rss_mb=200, cpu_pcts=[0, 0.5]))
    assert any(not e.ok and "RSS" in e.label for e in report.evaluations)
    assert report.all_ok is False


def test_swift_label_uses_40mb_budget() -> None:
    under = evaluate_budgets(
        _samples(rss_mb=30, cpu_pcts=[0, 0.5]),
        process_label="swift",
    )
    over = evaluate_budgets(
        _samples(rss_mb=60, cpu_pcts=[0, 0.5]),
        process_label="swift",
    )
    assert all(e.ok for e in under.evaluations if "RSS" in e.label)
    assert any(
        not e.ok and "RSS" in e.label for e in over.evaluations
    )


def test_cpu_mean_skips_first_zero_sample() -> None:
    # First sample is always 0 (no history); including it would bias
    # the mean downwards and let broken processes squeak past.
    report = evaluate_budgets(
        _samples(rss_mb=50, cpu_pcts=[0.0, 1.2, 1.1, 1.3])
    )
    cpu_eval = next(e for e in report.evaluations if "CPU" in e.label)
    # Mean of [1.2, 1.1, 1.3] ≈ 1.2 which is above 1% budget.
    assert cpu_eval.ok is False
    assert abs(cpu_eval.value - 1.2) < 0.01


def test_cpu_only_zero_sample_is_treated_as_idle() -> None:
    report = evaluate_budgets(_samples(rss_mb=50, cpu_pcts=[0.0]))
    cpu_eval = next(e for e in report.evaluations if "CPU" in e.label)
    assert cpu_eval.ok is True
    assert cpu_eval.value == 0.0


def test_cold_start_within_budget_passes() -> None:
    report = evaluate_budgets(
        _samples(rss_mb=50, cpu_pcts=[0, 0.5]),
        cold_start_s=0.8,
    )
    cs = next(e for e in report.evaluations if e.label == "cold start")
    assert cs.ok is True


def test_cold_start_over_budget_fails() -> None:
    report = evaluate_budgets(
        _samples(rss_mb=50, cpu_pcts=[0, 0.5]),
        cold_start_s=2.5,
    )
    cs = next(e for e in report.evaluations if e.label == "cold start")
    assert cs.ok is False
    assert report.all_ok is False


def test_ipc_p99_is_near_tail() -> None:
    rtts = [1.0] * 99 + [50.0]  # one outlier
    report = evaluate_budgets(
        _samples(rss_mb=50, cpu_pcts=[0, 0.5]),
        ipc_round_trips_ms=rtts,
    )
    p99 = next(e for e in report.evaluations if "IPC" in e.label)
    # p99 picks the outlier when n == 100.
    assert p99.value >= 50.0
    assert p99.ok is False


def test_empty_samples_produces_no_rss_cpu_evaluations() -> None:
    report = evaluate_budgets([], cold_start_s=0.5)
    labels = {e.label for e in report.evaluations}
    assert "cold start" in labels
    assert not any("RSS" in label for label in labels)
    assert not any("CPU" in label for label in labels)
    assert report.all_ok is True  # all present evaluations pass


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def test_format_report_marks_each_evaluation() -> None:
    report = evaluate_budgets(
        _samples(rss_mb=200, cpu_pcts=[0, 0.5]),
        cold_start_s=0.5,
    )
    text = format_report(report)
    # Peak RSS (200MB) should fail ❌; cold start should pass ✅.
    assert "❌" in text
    assert "✅" in text
    assert "Verdict:" in text


def test_format_report_handles_empty_evaluations() -> None:
    from deskmate_agent.perf import PerfReport

    text = format_report(PerfReport())
    assert "no evaluations" in text


# ---------------------------------------------------------------------------
# CPU-time parser (shared by the ps fallback on macOS)
# ---------------------------------------------------------------------------


def test_parse_cpu_time_mm_ss() -> None:
    assert _parse_cpu_time("1:30.50") == 90.5


def test_parse_cpu_time_hh_mm_ss() -> None:
    assert _parse_cpu_time("1:00:00") == 3600.0


def test_parse_cpu_time_bare_seconds() -> None:
    assert _parse_cpu_time("42.5") == 42.5


# ---------------------------------------------------------------------------
# Budget constants (defensive)
# ---------------------------------------------------------------------------


def test_budget_constants_match_plan_values() -> None:
    # V10 hard budgets — keep these pinned so a future refactor can't
    # silently loosen the targets. The list mirrors plan §3.1 row by
    # row so a missed budget here gets caught immediately.
    assert PYTHON_RSS_HARD_BUDGET_BYTES == 150 * 1024 * 1024
    assert SWIFT_RSS_HARD_BUDGET_BYTES == 40 * 1024 * 1024
    assert CPU_IDLE_HARD_BUDGET_PCT == 1.0
    assert CPU_HOT_HARD_BUDGET_PCT == 15.0
    assert COLD_START_HARD_BUDGET_S == 1.5
    assert WAKE_HARD_BUDGET_S == 0.5
    assert LLM_FIRST_TOKEN_HARD_BUDGET_S == 2.0
    assert FRAME_DROP_HARD_BUDGET_PCT == 0.0
    assert IPC_P99_HARD_BUDGET_MS == 10.0


# ---------------------------------------------------------------------------
# V10 §3.1 — newly wired budgets (CPU hot / wake / LLM / frame drop)
# ---------------------------------------------------------------------------


def test_hot_cpu_under_budget_passes() -> None:
    report = evaluate_budgets(
        _samples(rss_mb=50, cpu_pcts=[0, 0.5]),
        hot_cpu_pct=10.0,
    )
    hot = next(e for e in report.evaluations if e.label == "CPU hot")
    assert hot.ok is True
    assert hot.value == 10.0
    assert hot.detail == "under workload"


def test_hot_cpu_over_budget_fails() -> None:
    report = evaluate_budgets(
        _samples(rss_mb=50, cpu_pcts=[0, 0.5]),
        hot_cpu_pct=22.0,
    )
    hot = next(e for e in report.evaluations if e.label == "CPU hot")
    assert hot.ok is False
    assert report.all_ok is False


def test_wake_to_interactive_under_budget_passes() -> None:
    report = evaluate_budgets(
        _samples(rss_mb=50, cpu_pcts=[0, 0.5]),
        wake_s=0.3,
    )
    wake = next(
        e for e in report.evaluations if e.label == "wake to interactive"
    )
    assert wake.ok is True


def test_wake_to_interactive_over_budget_fails() -> None:
    report = evaluate_budgets(
        _samples(rss_mb=50, cpu_pcts=[0, 0.5]),
        wake_s=0.8,
    )
    wake = next(
        e for e in report.evaluations if e.label == "wake to interactive"
    )
    assert wake.ok is False
    assert report.all_ok is False


def test_llm_first_token_under_budget_passes() -> None:
    report = evaluate_budgets(
        _samples(rss_mb=50, cpu_pcts=[0, 0.5]),
        llm_first_token_s=1.2,
    )
    llm = next(
        e for e in report.evaluations if e.label == "LLM first token"
    )
    assert llm.ok is True


def test_llm_first_token_over_budget_fails() -> None:
    report = evaluate_budgets(
        _samples(rss_mb=50, cpu_pcts=[0, 0.5]),
        llm_first_token_s=2.5,
    )
    llm = next(
        e for e in report.evaluations if e.label == "LLM first token"
    )
    assert llm.ok is False
    assert report.all_ok is False


def test_frame_drop_zero_passes() -> None:
    """Plan target is exactly 0% — measured 0.0 must pass."""
    report = evaluate_budgets(
        _samples(rss_mb=50, cpu_pcts=[0, 0.5]),
        frame_drop_pct=0.0,
    )
    fd = next(e for e in report.evaluations if e.label == "frame drop")
    assert fd.ok is True
    assert fd.value == 0.0


def test_frame_drop_positive_fails() -> None:
    """Any non-zero drop trips the budget — there's no slack."""
    report = evaluate_budgets(
        _samples(rss_mb=50, cpu_pcts=[0, 0.5]),
        frame_drop_pct=0.5,
    )
    fd = next(e for e in report.evaluations if e.label == "frame drop")
    assert fd.ok is False
    assert report.all_ok is False


def test_all_nine_budgets_evaluate_when_supplied() -> None:
    """Smoke: feeding every metric should yield 9 evaluations
    (RSS / CPU mean / cold / hot / wake / LLM / frame / IPC) — and
    they all pass when each value is comfortably under budget."""
    report = evaluate_budgets(
        _samples(rss_mb=80, cpu_pcts=[0, 0.4, 0.5]),
        cold_start_s=0.9,
        ipc_round_trips_ms=[1.0, 2.0, 3.0],
        hot_cpu_pct=10.0,
        wake_s=0.2,
        llm_first_token_s=1.4,
        frame_drop_pct=0.0,
    )
    labels = [e.label for e in report.evaluations]
    # 2 from samples (RSS + CPU mean) + 7 from kwargs (cold start +
    # CPU hot + wake + LLM + frame drop + IPC) = 9 unique labels.
    assert "python RSS peak" in labels
    assert "python CPU (mean)" in labels
    assert "cold start" in labels
    assert "CPU hot" in labels
    assert "wake to interactive" in labels
    assert "LLM first token" in labels
    assert "frame drop" in labels
    assert "IPC p99" in labels
    assert report.all_ok is True


def test_partial_run_does_not_falsely_fail() -> None:
    """A Python-only run (no LLM, no Swift wake / frame drop)
    must stay green: missing kwargs == "not measured"."""
    report = evaluate_budgets(
        _samples(rss_mb=80, cpu_pcts=[0, 0.4]),
        cold_start_s=0.9,
    )
    labels = {e.label for e in report.evaluations}
    assert "wake to interactive" not in labels
    assert "LLM first token" not in labels
    assert "frame drop" not in labels
    assert "CPU hot" not in labels
    assert report.all_ok is True

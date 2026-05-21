#!/usr/bin/env bash
# Perf smoke test (V10 Phase 11).
#
# Launches the Python agent, waits for the ``agent_ready`` marker,
# then samples RSS + CPU for N seconds and reports pass / fail
# against the V10 hard budgets. Delegates everything to the Python
# harness so the sampling + budget-compare logic stays unit-testable.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
    if [[ -x "${REPO_ROOT}/agent/.venv/bin/python" ]]; then
        PYTHON_BIN="${REPO_ROOT}/agent/.venv/bin/python"
    else
        PYTHON_BIN="python3"
    fi
fi

exec "${PYTHON_BIN}" "${REPO_ROOT}/scripts/perf_smoke.py" "$@"

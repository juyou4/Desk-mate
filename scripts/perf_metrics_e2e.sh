#!/usr/bin/env bash
# End-to-end perf.metrics smoke (V10 §3.1 row 6 + row 8).
#
# Boots the Python agent + DeskmateShellApp daemon against a temp
# UDS, lets the binding push 3 envelopes, then verifies the agent
# logged a corresponding `app.perf_metrics` line. Cleans up after
# itself so it's safe to re-run.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOCK="/tmp/dm-perf-e2e.sock"
DB="/tmp/dm-perf-e2e-db"
AGENT_LOG="/tmp/dm-perf-e2e-agent.log"
SHELL_LOG="/tmp/dm-perf-e2e-shell.log"
RUN_SECS=12  # 2 envelopes at 5s pace + buffer

kill_pid() {
    local pid=$1
    local secs=${2:-3}
    [[ -z "${pid}" ]] && return 0
    kill -INT "${pid}" 2>/dev/null || return 0
    for _ in $(seq 1 $((secs * 4))); do
        kill -0 "${pid}" 2>/dev/null || return 0
        sleep 0.25
    done
    # Stubborn — escalate.
    kill -KILL "${pid}" 2>/dev/null || true
}

cleanup() {
    kill_pid "${SHELL_PID:-}" 3
    kill_pid "${AGENT_PID:-}" 3
    rm -f "${SOCK}"
}
trap cleanup EXIT

rm -rf "${SOCK}" "${DB}" "${AGENT_LOG}" "${SHELL_LOG}"
mkdir -p "${DB}"

export DESKMATE_SOCKET_PATH="${SOCK}"
export DESKMATE_DB_DIR="${DB}"

echo "[e2e] starting Python agent…"
PYTHON_BIN="${REPO_ROOT}/agent/.venv/bin/python"
if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "[e2e] FAIL: ${PYTHON_BIN} not found — create the venv first"
    exit 1
fi
# Run python directly (no subshell wrapper) so AGENT_PID is the
# real interpreter pid and SIGINT actually reaches it.
(cd "${REPO_ROOT}/agent" && exec "${PYTHON_BIN}" -m deskmate_agent) \
    > "${AGENT_LOG}" 2>&1 &
AGENT_PID=$!

# Wait up to 10s for agent_ready / started marker
for _ in $(seq 1 40); do
    if grep -q '"event": "deskmate_agent.started"' "${AGENT_LOG}" 2>/dev/null; then
        break
    fi
    sleep 0.25
done
if ! grep -q '"event": "deskmate_agent.started"' "${AGENT_LOG}" 2>/dev/null; then
    echo "[e2e] FAIL: agent never logged deskmate_agent.started"
    tail -n 40 "${AGENT_LOG}"
    exit 1
fi
echo "[e2e] agent up (pid=${AGENT_PID})"

SHELL_BIN="${REPO_ROOT}/DeskmateApp/.build/debug/DeskmateShellApp"
if [[ ! -x "${SHELL_BIN}" ]]; then
    echo "[e2e] FAIL: ${SHELL_BIN} not found — run 'swift build -c debug --product DeskmateShellApp' first"
    exit 1
fi
echo "[e2e] starting DeskmateShellApp…"
"${SHELL_BIN}" > "${SHELL_LOG}" 2>&1 &
SHELL_PID=$!

for i in $(seq 1 "${RUN_SECS}"); do
    echo "[e2e] tick ${i}/${RUN_SECS}"
    sleep 1
done

cleanup
trap - EXIT

echo
echo "=== Agent: deskmate_agent.started ==="
grep '"event": "deskmate_agent.started"' "${AGENT_LOG}" | head -n 1
echo
echo "=== Agent: app.perf_metrics ==="
grep '"event": "app.perf_metrics"' "${AGENT_LOG}" || true
PERF_COUNT=$(grep -c '"event": "app.perf_metrics"' "${AGENT_LOG}" || true)
echo
echo "=== Shell: bridge → connected ==="
grep 'bridge →' "${SHELL_LOG}" | head -n 5

echo
if [[ "${PERF_COUNT}" -ge 2 ]]; then
    echo "[e2e] PASS — got ${PERF_COUNT} app.perf_metrics records"
    exit 0
else
    echo "[e2e] FAIL — only ${PERF_COUNT} app.perf_metrics records (expected ≥2)"
    echo "----- agent log tail -----"
    tail -n 40 "${AGENT_LOG}"
    echo "----- shell log tail -----"
    tail -n 40 "${SHELL_LOG}"
    exit 1
fi

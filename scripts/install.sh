#!/usr/bin/env bash
# Developer install helper. Phase 0 placeholder: installs the Python agent in
# editable mode with dev + runtime extras. Real user install flow (.app DMG)
# lands in Phase 9+.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_DIR="$REPO_ROOT/agent"

python3 -m venv "$AGENT_DIR/.venv"
# shellcheck disable=SC1091
source "$AGENT_DIR/.venv/bin/activate"

pip install --upgrade pip
pip install -e "$AGENT_DIR[dev]"

echo "[install] Python agent installed in editable mode at $AGENT_DIR/.venv"
echo "[install] Activate with: source $AGENT_DIR/.venv/bin/activate"

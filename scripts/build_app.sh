#!/usr/bin/env bash
# Deskmate build: packages the Swift .app with an embedded Python agent.
# Phase 0 placeholder. Full py2app + xcodebuild pipeline lands in Phase 9+
# per the V10 unified plan.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "[build_app] Repo root: $REPO_ROOT"
echo "[build_app] Phase 0 placeholder — nothing to build yet."
echo "[build_app] Run 'pytest' in agent/ and 'swift test' in DeskmateApp/ to verify the scaffold."

#!/usr/bin/env bash
# scripts/install-git-hooks.sh — opt-in: wire `.githooks/` up as the
# repo's hooksPath so pre-commit runs scripts/test.sh automatically.
#
# Idempotent: safe to re-run. Does not touch anything outside this repo.
#
# Usage:
#   ./scripts/install-git-hooks.sh
#
# To disable later:
#   git config --unset core.hooksPath
set -euo pipefail

cd "$(dirname "$0")/.."

git config core.hooksPath .githooks
chmod +x .githooks/pre-commit scripts/test.sh 2>/dev/null || true

echo "hooks installed — pre-commit will now run scripts/test.sh"
echo "to disable: git config --unset core.hooksPath"

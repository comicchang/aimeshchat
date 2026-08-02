#!/usr/bin/env bash
# check-plugin-types.sh — Typecheck + test gate for omp-mailbox-plugin
# Usage: ./scripts/check-plugin-types.sh [plugin-repo-path]
# Default plugin repo: ../omp-mailbox-plugin relative to codeagent-py root

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="${1:-$(cd "$SCRIPT_DIR/../.." && echo "$PWD")/omp-mailbox-plugin}"

if [[ ! -d "$PLUGIN_DIR" ]]; then
  echo "❌ Plugin repo not found at: $PLUGIN_DIR" >&2
  exit 1
fi

if [[ ! -f "$PLUGIN_DIR/package.json" ]]; then
  echo "❌ No package.json in: $PLUGIN_DIR" >&2
  exit 1
fi

cd "$PLUGIN_DIR"

echo "━━━ Installing dependencies ━━━"
bun install

echo ""
echo "━━━ Running typecheck ━━━"
bun run typecheck

echo ""
echo "━━━ Running tests ━━━"
bun test

echo ""
echo "✅ All gates passed"

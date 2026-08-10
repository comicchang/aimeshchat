#!/bin/bash
# codeagent-py development bootstrap — NOT for production deployment.
#
# Production: run `dotai setup` (handles clone, uv tool install, skill linking).
# This script is for local development checkout only.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=== codeagent-py development setup ==="

# 1. Install package via uv
echo "[1/3] Installing codeagent package..."
if ! command -v uv >/dev/null 2>&1; then
    echo "  ✗ uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi
cd "$PROJECT_DIR"
uv tool install . --force 2>&1
echo "  ✓ codeagent installed"

# 2. Verify CLI
echo "[2/3] Verifying CLI..."
if ! postmesh --version >/dev/null 2>&1; then
    echo "  ✗ postmesh not in PATH after install." >&2
    echo "  Run: export PATH=\"\$HOME/.local/bin:\$PATH\"" >&2
    exit 1
fi
echo "  ✓ $(postmesh --version)"

# 3. Verify remote exec helper
echo "[3/3] Verifying remote exec helper..."
if ! postmesh-remote-exec --help >/dev/null 2>&1; then
    echo "  ✗ postmesh-remote-exec not in PATH." >&2
    exit 1
fi
echo "  ✓ postmesh-remote-exec available"

echo ""
echo "=== Development setup complete ==="
echo ""
echo "Production deployment: run 'dotai setup' on each machine."
echo "Edit ~/.config/codeagent/repo-map.json with your host configurations."

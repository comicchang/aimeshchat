#!/bin/bash
# DEPRECATED: local `uv tool install .` conflicts with dotai setup's pinned git ref.
#
# dotai setup installs aimeshchat from the pinned git tag (v0.2.8) via
# `uv tool install git+https://...@v0.2.8 --force`. A local path install
# (`uv tool install . --force`) creates a different uv tracking entry that
# silently overrides the pinned version, causing version drift.
#
# Use `dotai setup` instead — it handles clone, uv tool install, skill linking,
# and version pinning atomically.
set -euo pipefail

echo "ERROR: scripts/setup.sh is disabled." >&2
echo "" >&2
echo "Local 'uv tool install .' conflicts with dotai setup's pinned git ref." >&2
echo "Use 'dotai setup' instead — it installs the correct pinned version." >&2
echo "" >&2
echo "If you need a local dev install for testing, run manually:" >&2
echo "  uv tool install . --force" >&2
echo "" >&2
echo "WARNING: this will override the dotai-pinned version." >&2
exit 1

#!/bin/bash
# codeagent-py setup — install package + skills + config
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=== codeagent-py setup ==="

# 1. Install package
echo "[1/5] Installing codeagent package..."
cd "$PROJECT_DIR"
if command -v uv >/dev/null 2>&1; then
    uv tool install . --force 2>&1 || pip install -e . --user 2>&1
else
    pip install -e . --user 2>&1
fi
echo "  ✓ codeagent installed"

# 2. Verify CLI
echo "[2/5] Verifying CLI..."
if codeagent --version >/dev/null 2>&1; then
    echo "  ✓ $(codeagent --version)"
else
    echo "  ✗ codeagent not in PATH — add ~/.local/bin to PATH"
fi

# 3. Create config directory
echo "[3/5] Setting up config..."
mkdir -p ~/.config/codeagent
if [ ! -f ~/.config/codeagent/repo-map.json ]; then
    if [ -f "$PROJECT_DIR/examples/repo-map.json" ]; then
        cp "$PROJECT_DIR/examples/repo-map.json" ~/.config/codeagent/repo-map.json
        echo "  ✓ Created ~/.config/codeagent/repo-map.json (from example)"
        echo "    ⚠ Edit this file with your actual host configurations"
    fi
else
    echo "  ✓ ~/.config/codeagent/repo-map.json already exists"
fi

# 4. Link skills to dotai local-skills
echo "[4/5] Linking skills..."
DOTAI_SKILLS="${HOME}/src/dotai/external/local-skills"
if [ -d "$DOTAI_SKILLS" ]; then
    for skill_dir in "$PROJECT_DIR/skills"/*/; do
        skill_name=$(basename "$skill_dir")
        target="$DOTAI_SKILLS/$skill_name"
        if [ -L "$target" ]; then
            echo "  ✓ $skill_name already linked"
        elif [ -d "$target" ]; then
            echo "  ⚠ $skill_name exists as directory (not symlink), skipping"
        else
            ln -s "$skill_dir" "$target"
            echo "  ✓ Linked $skill_name → $skill_dir"
        fi
    done
else
    echo "  ⚠ dotai local-skills not found at $DOTAI_SKILLS, skipping skill linking"
fi

# 5. Remote deploy helper
REPO_URL="https://github.com/comicchang/codeagent-py"
echo "[5/5] Remote deploy info:"
echo "  On each remote host, run:"
echo "    pip install git+${REPO_URL}"
echo "  Or for development:"
echo "    git clone ${REPO_URL} ~/src/codeagent-py && cd ~/src/codeagent-py && pip install -e ."

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit ~/.config/codeagent/repo-map.json with your hosts"
echo "  2. Deploy to remote hosts: ssh <host> 'cd ~/src/codeagent-py && pip install -e .'"
echo "  3. Test: codeagent route list"
echo "  4. Test: codeagent run 'echo hello' --host <alias>"

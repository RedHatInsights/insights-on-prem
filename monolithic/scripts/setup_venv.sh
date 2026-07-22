#!/bin/bash
#
# setup_venv.sh — Create a Python venv with molodec installed.
#
# Uses uv for fast venv creation and package install.
# Molodec is fetched from the internal Red Hat PyPI.
#
# Usage:
#   ./scripts/setup_venv.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"

if [ -d "$VENV_DIR" ]; then
    echo "venv already exists at $VENV_DIR"
    echo "To recreate, remove it first: rm -rf $VENV_DIR"
    exit 0
fi

if ! command -v uv &>/dev/null; then
    echo "ERROR: uv not found. Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

echo "Creating venv at $VENV_DIR ..."
uv venv "$VENV_DIR"

echo "Installing molodec ..."
UV_NATIVE_TLS=1 uv pip install --python "$VENV_DIR/bin/python3" \
    --index-url https://nexus.corp.redhat.com/repository/obsint-pypi/simple \
    -U molodec

echo ""
echo "Done. venv ready at $VENV_DIR"
echo "reproduce_leak.sh will use it automatically."

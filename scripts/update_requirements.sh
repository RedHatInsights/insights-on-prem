#!/bin/bash
# Regenerate requirements.txt and requirements-build.txt from requirements-in.txt.
#
# Must run on linux/amd64 with Python 3.12 (matching the Dockerfile / Konflux
# platform). On macOS/arm64, pip-compile silently drops packages whose markers
# only match x86_64/aarch64 (e.g. greenlet). Prefer:
#
#   podman run --rm --platform linux/amd64 -v "$(pwd):/work:Z" -w /work \
#     python:3.12-slim bash scripts/update_requirements.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "${REPO_ROOT}"

if [ "$(uname -s)" != "Linux" ] || [ "$(uname -m)" != "x86_64" ]; then
    echo "Error: must run on Linux x86_64 (got $(uname -s)/$(uname -m))." >&2
    echo "Use: podman run --rm --platform linux/amd64 -v \"\$(pwd):/work:Z\" -w /work python:3.12-slim bash scripts/update_requirements.sh" >&2
    exit 1
fi

PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [ "${PY_VER}" != "3.12" ]; then
    echo "Error: must run with Python 3.12 (got ${PY_VER})." >&2
    echo "Use: podman run --rm --platform linux/amd64 -v \"\$(pwd):/work:Z\" -w /work python:3.12-slim bash scripts/update_requirements.sh" >&2
    exit 1
fi

python3 -m pip install -q pip-tools pybuild-deps
pip-compile --output-file=requirements.txt requirements-in.txt
pybuild-deps compile --generate-hashes --output-file=requirements-build.txt requirements.txt

echo "Updated requirements.txt and requirements-build.txt"

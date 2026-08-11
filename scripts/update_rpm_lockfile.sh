#!/bin/bash

# Update rpms.lock.yaml using rpm-lockfile-prototype.
#
# Must run on linux/amd64 inside a UBI9/RHEL9 environment with
# subscription-manager (matching Konflux's prefetch platform). Prefer:
#
#   podman run --rm --platform linux/amd64 \
#     -v "$(pwd):/work:Z" -w /work \
#     -e RH_ORG_ID -e RH_ACTIVATION_KEY \
#     registry.access.redhat.com/ubi9/ubi \
#     bash scripts/update_rpm_lockfile.sh
#
# Requires:
#   RH_ORG_ID, RH_ACTIVATION_KEY
#   scripts/.dockerconfig.json  (registry.redhat.io pulls via skopeo)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
INPUT_FILE="${REPO_ROOT}/rpms.in.yaml"
OUTPUT_FILE="${REPO_ROOT}/rpms.lock.yaml"
DOCKERFILE="${REPO_ROOT}/Dockerfile"
DOCKERCONFIG_FILE="${SCRIPT_DIR}/.dockerconfig.json"
PODMAN_HINT="podman run --rm --platform linux/amd64 -v \"\$(pwd):/work:Z\" -w /work -e RH_ORG_ID -e RH_ACTIVATION_KEY registry.access.redhat.com/ubi9/ubi bash scripts/update_rpm_lockfile.sh"

cd "${REPO_ROOT}"

if [ "$(uname -s)" != "Linux" ] || [ "$(uname -m)" != "x86_64" ]; then
    echo "Error: must run on Linux x86_64 (got $(uname -s)/$(uname -m))." >&2
    echo "Use: ${PODMAN_HINT}" >&2
    exit 1
fi

if ! command -v subscription-manager >/dev/null 2>&1; then
    echo "Error: subscription-manager not found (need entitlement-capable RHEL9 or ubi9/ubi)." >&2
    echo "Use: ${PODMAN_HINT}" >&2
    exit 1
fi

if [ ! -f "${INPUT_FILE}" ]; then
    echo "Error: Input file not found: ${INPUT_FILE}" >&2
    exit 1
fi

if [ ! -f "${DOCKERFILE}" ]; then
    echo "Error: Dockerfile not found: ${DOCKERFILE}" >&2
    exit 1
fi

missing=()
[ -z "${RH_ORG_ID:-}" ] && missing+=("RH_ORG_ID")
[ -z "${RH_ACTIVATION_KEY:-}" ] && missing+=("RH_ACTIVATION_KEY")
if [ ${#missing[@]} -gt 0 ]; then
    echo "Error: Missing required env vars: ${missing[*]}" >&2
    exit 1
fi

if [ ! -f "${DOCKERCONFIG_FILE}" ]; then
    echo "Error: Registry auth file not found: ${DOCKERCONFIG_FILE}" >&2
    echo "Required so skopeo can pull from registry.redhat.io." >&2
    exit 1
fi

echo "Updating RPM lockfile..."
echo "Input:  ${INPUT_FILE}"
echo "Output: ${OUTPUT_FILE}"
echo ""

# Unregister only if this run successfully registered, so we don't tear down a
# pre-existing host/container registration on early failure. Always remove the
# copied redhat.repo so repeated runs don't leave entitled repo metadata behind.
REGISTERED=0
cleanup() {
    rm -f "${REPO_ROOT}/redhat.repo"
    if [ "${REGISTERED}" -eq 1 ]; then
        subscription-manager unregister || true
    fi
}
trap cleanup EXIT

subscription-manager register --org="${RH_ORG_ID}" --activationkey="${RH_ACTIVATION_KEY}"
REGISTERED=1
subscription-manager refresh
# Activation keys often enable EUS repos by default. Those resolve $releasever
# to "9" and 404 on the CDN (eus/rhel9/9/...), so drop them and use the regular
# dist repos instead.
subscription-manager repos --disable '*-eus-*' || true
subscription-manager repos --enable rhel-9-for-x86_64-baseos-rpms
subscription-manager repos --enable rhel-9-for-x86_64-appstream-rpms
subscription-manager repos --enable codeready-builder-for-rhel-9-x86_64-rpms

dnf install -y python3-pip skopeo git
python3 -m pip install --user git+https://github.com/konflux-ci/rpm-lockfile-prototype.git

export REGISTRY_AUTH_FILE="${DOCKERCONFIG_FILE}"

# rpm-lockfile-prototype reads redhat.repo from the working directory.
/usr/bin/cp -f /etc/yum.repos.d/redhat.repo "${REPO_ROOT}/redhat.repo"

~/.local/bin/rpm-lockfile-prototype rpms.in.yaml --outfile rpms.lock.yaml

if [ ! -s "${OUTPUT_FILE}" ]; then
    echo "Error: Output file is empty or was not created" >&2
    exit 1
fi

echo "Successfully updated ${OUTPUT_FILE}"

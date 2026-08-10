#!/bin/bash

# Adapted from rules-containers/scripts/update_rpm_lockfile.sh

# Update rpms.lock.yaml using rpm-lockfile-prototype.
#
# This script generates the RPM lockfile from rpms.in.yaml using
# rpm-lockfile-prototype inside a container registered with subscription-manager,
# so it has access to the full entitled RHEL CDN (e.g. postgresql-devel, which is
# not published on the public UBI CDN).
#
# Authentication (pick one; all values via env vars):
#   Username/password:
#     RH_USER=<username> PASSWORD=<password>
#   Org ID + activation key:
#     RH_ORG_ID=<org_id> RH_ACTIVATION_KEY=<activation_key>
#     Also requires scripts/.dockerconfig.json for registry.redhat.io pulls.

set -e

# Get script directory and repo root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
INPUT_FILE="${REPO_ROOT}/rpms.in.yaml"
OUTPUT_FILE="${REPO_ROOT}/rpms.lock.yaml"
DOCKERFILE="${REPO_ROOT}/Dockerfile"
DOCKERCONFIG_FILE="${SCRIPT_DIR}/.dockerconfig.json"

# Prerequisites
if [ ! -f "${INPUT_FILE}" ]; then
    echo "Error: Input file not found: ${INPUT_FILE}" >&2
    exit 1
fi

if [ ! -f "${DOCKERFILE}" ]; then
    echo "Error: Dockerfile not found: ${DOCKERFILE}" >&2
    exit 1
fi

if command -v podman >/dev/null 2>&1; then
    CONTAINER_CMD="podman"
elif command -v docker >/dev/null 2>&1; then
    CONTAINER_CMD="docker"
else
    echo "Error: docker or podman not found in PATH" >&2
    exit 1
fi

# Resolve authentication: username/password or org_id + activation-key
if [ -n "${RH_ORG_ID}" ] || [ -n "${RH_ACTIVATION_KEY}" ]; then
    AUTH_MODE="activationkey"
    missing=()
    [ -z "${RH_ORG_ID}" ] && missing+=("RH_ORG_ID")
    [ -z "${RH_ACTIVATION_KEY}" ] && missing+=("RH_ACTIVATION_KEY")
    if [ ${#missing[@]} -gt 0 ]; then
        echo "Error: Missing required env vars for activation-key mode: ${missing[*]}" >&2
        exit 1
    fi
    if [ ! -f "${DOCKERCONFIG_FILE}" ]; then
        echo "Error: Registry auth file not found: ${DOCKERCONFIG_FILE}" >&2
        echo "Required for activation-key mode so skopeo can pull from registry.redhat.io." >&2
        exit 1
    fi
    SUB_MGR_CMD="subscription-manager register --org=\${RH_ORG_ID} --activationkey=\${RH_ACTIVATION_KEY}"
    # Org/activation-key are not valid registry credentials; use the dockerconfig instead.
    SKOPEO_LOGIN_CMD="export REGISTRY_AUTH_FILE=/source/scripts/.dockerconfig.json"
elif [ -n "${RH_USER}" ] || [ -n "${PASSWORD}" ]; then
    AUTH_MODE="password"
    missing=()
    [ -z "${RH_USER}" ] && missing+=("RH_USER")
    [ -z "${PASSWORD}" ] && missing+=("PASSWORD")
    if [ ${#missing[@]} -gt 0 ]; then
        echo "Error: Missing required env vars for password mode: ${missing[*]}" >&2
        exit 1
    fi
    SUB_MGR_CMD="subscription-manager register --username=\"\${RH_USER}\" --password=\"\${PASSWORD}\""
    SKOPEO_LOGIN_CMD="skopeo login registry.redhat.io -u \"\$RH_USER\" -p \"\$PASSWORD\""
else
    echo "Error: Set RH_ORG_ID+RH_ACTIVATION_KEY or RH_USER+PASSWORD" >&2
    exit 1
fi

echo "Using ${CONTAINER_CMD} to update RPM lockfile..."
echo "Auth:   ${AUTH_MODE}"
echo "Input:  ${INPUT_FILE}"
echo "Output: ${OUTPUT_FILE}"
echo ""

# Run the container from the repo root so paths work correctly
cd "${REPO_ROOT}"

# Force x86_64 platform to ensure consistent repo configuration across host architectures.
# No TTY (-t): credentials come from env vars; this script is always non-interactive.
CONTAINER_ARGS=(
    "run" "--rm" "-i"
    "--platform" "linux/amd64"
    "-v" "$(pwd):/source:Z"
)

# Pass credentials into the container
if [ "${AUTH_MODE}" = "activationkey" ]; then
    CONTAINER_ARGS+=("-e" "RH_ORG_ID=${RH_ORG_ID}")
    CONTAINER_ARGS+=("-e" "RH_ACTIVATION_KEY=${RH_ACTIVATION_KEY}")
else
    CONTAINER_ARGS+=("-e" "RH_USER=${RH_USER}")
    CONTAINER_ARGS+=("-e" "PASSWORD=${PASSWORD}")
fi

# Add image
CONTAINER_ARGS+=("registry.access.redhat.com/ubi9")

# Build the bash command to run inside the container
# `set -e` + the unregister trap matter: without them, a failure partway
# through (e.g. rpm-lockfile-prototype erroring out) would still let the
# script reach `rm -rf redhat.repo` and exit 0, making the outer script
# falsely report success while leaving the old rpms.lock.yaml untouched.
# The trap also unregisters the subscription-manager system on every exit
# (success or failure) so repeated runs don't leak registered systems and
# eventually exhaust the account's subscription/system-registration limit
# (surfaces as "This system has no repositories available through
# subscriptions." on a later run).
BASH_CMD=$(cat <<EOF
set -e
trap 'subscription-manager unregister || true' EXIT
${SUB_MGR_CMD}
subscription-manager refresh
# Activation keys often enable EUS repos by default. Those resolve
# \$releasever to "9" and 404 on the CDN (eus/rhel9/9/...), so drop them
# and use the regular dist repos instead.
subscription-manager repos --disable '*-eus-*' || true
subscription-manager repos --enable rhel-9-for-x86_64-baseos-rpms
subscription-manager repos --enable rhel-9-for-x86_64-appstream-rpms
subscription-manager repos --enable codeready-builder-for-rhel-9-x86_64-rpms
dnf install -y pip skopeo git
pip install --user git+https://github.com/konflux-ci/rpm-lockfile-prototype.git
${SKOPEO_LOGIN_CMD}
/usr/bin/cp -f /etc/yum.repos.d/redhat.repo /source/redhat.repo
cd /source
~/.local/bin/rpm-lockfile-prototype rpms.in.yaml --outfile rpms.lock.yaml
rm -rf /source/redhat.repo
EOF
)

# Run the container with all setup and execution commands
"${CONTAINER_CMD}" "${CONTAINER_ARGS[@]}" "bash" "-c" "${BASH_CMD}"

# Verify the output file was created and is not empty
if [ ! -s "${OUTPUT_FILE}" ]; then
    echo "Error: Output file is empty or was not created" >&2
    exit 1
fi

echo "Successfully updated ${OUTPUT_FILE}"

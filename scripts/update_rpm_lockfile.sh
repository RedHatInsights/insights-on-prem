#!/bin/bash

# Adapted from rules-containers/scripts/update_rpm_lockfile.sh

# Update monolithic/rpms.lock.yaml using rpm-lockfile-prototype.
#
# This script generates the RPM lockfile from monolithic/rpms.in.yaml using
# rpm-lockfile-prototype inside a container registered with subscription-manager,
# so it has access to the full entitled RHEL CDN (e.g. postgresql-devel, which is
# not published on the public UBI CDN).

set -e

# Get script directory and repo root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
RPM_PREFETCH_DIR="${REPO_ROOT}/monolithic"
INPUT_FILE="${RPM_PREFETCH_DIR}/rpms.in.yaml"
OUTPUT_FILE="${RPM_PREFETCH_DIR}/rpms.lock.yaml"
DOCKERFILE="${RPM_PREFETCH_DIR}/Dockerfile"

# Check if input file exists
if [ ! -f "${INPUT_FILE}" ]; then
    echo "Error: Input file not found: ${INPUT_FILE}" >&2
    exit 1
fi

# Check if Dockerfile exists
if [ ! -f "${DOCKERFILE}" ]; then
    echo "Error: Dockerfile not found: ${DOCKERFILE}" >&2
    exit 1
fi

# Prompt for RH_USER if empty
if [ -z "${RH_USER}" ]; then
    read -r -p "Enter Red Hat username: " RH_USER
    if [ -z "${RH_USER}" ]; then
        echo "Error: Red Hat username cannot be empty" >&2
        exit 1
    fi
fi

# Prompt for password
read -rs -p "Enter password for ${RH_USER}: " PASSWORD
echo ""
if [ -z "${PASSWORD}" ]; then
    echo "Error: Password cannot be empty" >&2
    exit 1
fi

# Require docker or podman
if command -v podman >/dev/null 2>&1; then
    CONTAINER_CMD="podman"
elif command -v docker >/dev/null 2>&1; then
    CONTAINER_CMD="docker"
else
    echo "Error: docker or podman not found in PATH" >&2; exit 1
fi

# Build subscription-manager register command
SUB_MGR_CMD="subscription-manager register --username=\${RH_USER} --password=\${PASSWORD}"

echo "Using ${CONTAINER_CMD} to update RPM lockfile..."
echo "Input:  ${INPUT_FILE}"
echo "Output: ${OUTPUT_FILE}"
echo ""

# Run the container from the repo root so paths work correctly
cd "${REPO_ROOT}"

# Build container command arguments
# Force x86_64 platform to ensure consistent repo configuration across host architectures
CONTAINER_ARGS=(
    "run" "--rm" "-it"
    "--platform" "linux/amd64"
    "-v" "$(pwd):/source:Z"
)

# Add environment variables
CONTAINER_ARGS+=("-e" "RH_USER=${RH_USER}")
CONTAINER_ARGS+=("-e" "PASSWORD=${PASSWORD}")

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
dnf install -y pip skopeo git
pip install --user git+https://github.com/konflux-ci/rpm-lockfile-prototype.git
skopeo login registry.redhat.io -u \$RH_USER -p \$PASSWORD
subscription-manager repos --enable codeready-builder-for-rhel-9-x86_64-rpms
/usr/bin/cp -f /etc/yum.repos.d/redhat.repo /source/monolithic/redhat.repo
cd /source
~/.local/bin/rpm-lockfile-prototype monolithic/rpms.in.yaml --outfile monolithic/rpms.lock.yaml
rm -rf /source/monolithic/redhat.repo
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

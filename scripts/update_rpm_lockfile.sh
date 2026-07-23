#!/bin/bash

# Adapted from rules-containers/scripts/update_rpm_lockfile.sh

# Update monolithic/rpms.lock.yaml using rpm-lockfile-prototype.
#
# This script generates the RPM lockfile from monolithic/rpms.in.yaml using
# rpm-lockfile-prototype inside a container registered with subscription-manager,
# so it has access to the full entitled RHEL CDN (e.g. postgresql-devel, which is
# not published on the public UBI CDN).
#
# Authentication (pick one):
#   Username/password:
#     RH_USER=<username>  (password prompted if unset)
#   Org ID + activation key:
#     RH_ORG_ID=<org_id> RH_ACTIVATION_KEY=<activation_key>
#     Also requires scripts/.dockerconfig.json for registry.redhat.io pulls.

set -e

# Get script directory and repo root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
RPM_PREFETCH_DIR="${REPO_ROOT}/monolithic"
INPUT_FILE="${RPM_PREFETCH_DIR}/rpms.in.yaml"
OUTPUT_FILE="${RPM_PREFETCH_DIR}/rpms.lock.yaml"
DOCKERFILE="${RPM_PREFETCH_DIR}/Dockerfile"
DOCKERCONFIG_FILE="${SCRIPT_DIR}/.dockerconfig.json"

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

# Resolve authentication: username/password or org_id + activation-key
AUTH_MODE=""
if [ -n "${RH_ORG_ID}" ] || [ -n "${RH_ACTIVATION_KEY}" ]; then
    AUTH_MODE="activationkey"
elif [ -n "${RH_USER}" ]; then
    AUTH_MODE="password"
else
    echo "Select Red Hat authentication method:"
    echo "  1) Username / password"
    echo "  2) Organization ID / activation key"
    read -r -p "Choice [1/2]: " AUTH_CHOICE
    case "${AUTH_CHOICE}" in
        2) AUTH_MODE="activationkey" ;;
        *) AUTH_MODE="password" ;;
    esac
fi

if [ "${AUTH_MODE}" = "activationkey" ]; then
    if [ -z "${RH_ORG_ID}" ]; then
        read -r -p "Enter Red Hat organization ID: " RH_ORG_ID
        if [ -z "${RH_ORG_ID}" ]; then
            echo "Error: Organization ID cannot be empty" >&2
            exit 1
        fi
    fi
    if [ -z "${RH_ACTIVATION_KEY}" ]; then
        read -rs -p "Enter activation key: " RH_ACTIVATION_KEY
        echo ""
        if [ -z "${RH_ACTIVATION_KEY}" ]; then
            echo "Error: Activation key cannot be empty" >&2
            exit 1
        fi
    fi
    if [ ! -f "${DOCKERCONFIG_FILE}" ]; then
        echo "Error: Registry auth file not found: ${DOCKERCONFIG_FILE}" >&2
        echo "Required for activation-key mode so skopeo can pull from registry.redhat.io." >&2
        exit 1
    fi
    SUB_MGR_CMD="subscription-manager register --org=\${RH_ORG_ID} --activationkey=\${RH_ACTIVATION_KEY}"
    # Org/activation-key are not valid registry credentials; use the dockerconfig instead.
    SKOPEO_LOGIN_CMD="export REGISTRY_AUTH_FILE=/source/scripts/.dockerconfig.json"
else
    if [ -z "${RH_USER}" ]; then
        read -r -p "Enter Red Hat username: " RH_USER
        if [ -z "${RH_USER}" ]; then
            echo "Error: Red Hat username cannot be empty" >&2
            exit 1
        fi
    fi
    if [ -z "${PASSWORD}" ]; then
        read -rs -p "Enter password for ${RH_USER}: " PASSWORD
        echo ""
        if [ -z "${PASSWORD}" ]; then
            echo "Error: Password cannot be empty" >&2
            exit 1
        fi
    fi
    SUB_MGR_CMD="subscription-manager register --username=\${RH_USER} --password=\${PASSWORD}"
    SKOPEO_LOGIN_CMD="skopeo login registry.redhat.io -u \$RH_USER -p \$PASSWORD"
fi

# Require docker or podman
if command -v podman >/dev/null 2>&1; then
    CONTAINER_CMD="podman"
elif command -v docker >/dev/null 2>&1; then
    CONTAINER_CMD="docker"
else
    echo "Error: docker or podman not found in PATH" >&2; exit 1
fi

echo "Using ${CONTAINER_CMD} to update RPM lockfile..."
echo "Auth:   ${AUTH_MODE}"
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

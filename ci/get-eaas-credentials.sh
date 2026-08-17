#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="obsint-processing-tenant"
COMPONENT="insights-on-prem"
ITS_NAME="insights-on-prem-eaas-e2e"

usage() {
  echo "Usage: $0 [pipelinerun-name]"
  echo ""
  echo "Extracts EaaS cluster credentials from the hold-clusters-on-failure"
  echo "finally task and creates two oc contexts: eaas-hub and eaas-managed."
  echo ""
  echo "If no PipelineRun name is given, finds the latest one with the"
  echo "debug.iop/hold-on-failure=true label."
  echo ""
  echo "Requires: oc (logged into the Konflux cluster)"
  exit 1
}

[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && usage

find_pipelinerun() {
  oc get pipelinerun -n "${NAMESPACE}" \
    -l "test.appstudio.openshift.io/scenario=${ITS_NAME},debug.iop/hold-on-failure=true" \
    --sort-by=.metadata.creationTimestamp \
    -o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null || true
}

PIPELINE_RUN="${1:-}"

if [[ -z "${PIPELINE_RUN}" ]]; then
  echo "Looking for latest debug PipelineRun..."
  PIPELINE_RUN=$(find_pipelinerun)
  if [[ -z "${PIPELINE_RUN}" ]]; then
    echo "ERROR: No PipelineRun found with debug.iop/hold-on-failure=true"
    echo "Either pass the PipelineRun name as argument or trigger with:"
    echo "  ci/trigger-e2e.sh --debug"
    exit 1
  fi
fi

echo "PipelineRun: ${PIPELINE_RUN}"

POD="${PIPELINE_RUN}-hold-clusters-on-failure-pod"
CONTAINER="step-print-credentials-and-wait"

echo "Fetching credentials from ${POD}..."

LOGS=$(oc logs -n "${NAMESPACE}" "${POD}" -c "${CONTAINER}" 2>/dev/null) || {
  echo "ERROR: Could not get logs from ${POD} container ${CONTAINER}"
  echo ""
  echo "The hold-clusters-on-failure task may not have run yet (pipeline"
  echo "needs to fail first) or the pod may have been cleaned up."
  echo ""
  echo "Check pod status:"
  echo "  oc get pod ${POD} -n ${NAMESPACE}"
  exit 1
}

if echo "${LOGS}" | grep -q "Hold not requested"; then
  echo "ERROR: hold-on-failure was not active for this PipelineRun."
  echo "The debug.iop/hold-on-failure label may have been added too late."
  exit 1
fi

parse_field() {
  local section="$1" field="$2"
  echo "${LOGS}" | awk -v section="${section}" -v field="${field}" '
    $0 ~ "=== " section " ===" { in_section=1; next }
    $0 ~ /^=== .* ===$/ { in_section=0 }
    in_section && $0 ~ "^" field ":" { sub("^" field ":[ ]*", ""); print; exit }
  '
}

HUB_API=$(parse_field "HUB CLUSTER" "API Server")
HUB_USER=$(parse_field "HUB CLUSTER" "Username")
HUB_PASS=$(parse_field "HUB CLUSTER" "Password")
HUB_CONSOLE=$(parse_field "HUB CLUSTER" "Console")

MANAGED_API=$(parse_field "MANAGED CLUSTER" "API Server")
MANAGED_USER=$(parse_field "MANAGED CLUSTER" "Username")
MANAGED_PASS=$(parse_field "MANAGED CLUSTER" "Password")
MANAGED_CONSOLE=$(parse_field "MANAGED CLUSTER" "Console")

for var in HUB_API HUB_USER HUB_PASS MANAGED_API MANAGED_USER MANAGED_PASS; do
  if [[ -z "${!var}" ]]; then
    echo "ERROR: Could not parse ${var} from task logs."
    echo "The log format may have changed. Raw logs:"
    echo "${LOGS}"
    exit 1
  fi
done

echo ""
echo "--- Hub cluster ---"
echo "  API:     ${HUB_API}"
echo "  Console: ${HUB_CONSOLE}"
echo "  User:    ${HUB_USER}"
echo ""
echo "--- Managed cluster ---"
echo "  API:     ${MANAGED_API}"
echo "  Console: ${MANAGED_CONSOLE}"
echo "  User:    ${MANAGED_USER}"
echo ""

ORIGINAL_CTX=$(oc config current-context 2>/dev/null || true)

# Remove old eaas contexts/clusters/users if they exist
for name in eaas-hub eaas-managed; do
  oc config delete-context "${name}" 2>/dev/null || true
  oc config delete-cluster "${name}" 2>/dev/null || true
  oc config delete-user "${name}" 2>/dev/null || true
done

echo "Logging into hub cluster..."
oc login "${HUB_API}" \
  -u "${HUB_USER}" -p "${HUB_PASS}" \
  --insecure-skip-tls-verify \
  --kubeconfig="${HOME}/.kube/config" >/dev/null

HUB_CTX=$(oc config current-context)
oc config rename-context "${HUB_CTX}" eaas-hub >/dev/null

echo "Logging into managed cluster..."
oc login "${MANAGED_API}" \
  -u "${MANAGED_USER}" -p "${MANAGED_PASS}" \
  --insecure-skip-tls-verify \
  --kubeconfig="${HOME}/.kube/config" >/dev/null

MANAGED_CTX=$(oc config current-context)
oc config rename-context "${MANAGED_CTX}" eaas-managed >/dev/null

if [[ -n "${ORIGINAL_CTX}" ]]; then
  oc config use-context "${ORIGINAL_CTX}" >/dev/null
fi

echo ""
echo "Contexts created:"
echo "  oc config use-context eaas-hub"
echo "  oc config use-context eaas-managed"
echo ""
echo "=== HUB CLUSTER ==="
echo "  Console:  ${HUB_CONSOLE}"
echo "  Username: ${HUB_USER}"
echo "  Password: ${HUB_PASS}"
echo ""
echo "=== MANAGED CLUSTER ==="
echo "  Console:  ${MANAGED_CONSOLE}"
echo "  Username: ${MANAGED_USER}"
echo "  Password: ${MANAGED_PASS}"
echo ""
echo "Cancel the PipelineRun when done:"
echo "  oc patch pipelinerun ${PIPELINE_RUN} -n ${NAMESPACE} --type merge -p '{\"spec\":{\"status\":\"CancelledRunFinally\"}}'"

#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="obsint-processing-tenant"
ITS_NAME="insights-on-prem-eaas-e2e"

usage() {
  echo "Usage: $0 [pipelinerun-name]"
  echo ""
  echo "Reads the kubeconfigs produced by the provision-ephemeral-cluster tasks"
  echo "and creates two oc contexts: eaas-hub and eaas-managed."
  echo ""
  echo "If no PipelineRun name is given, finds the latest one with the"
  echo "debug.iop/hold-on-failure=true label."
  echo ""
  echo "Note: the ephemeral clusters are HyperShift hosted clusters. They have"
  echo "no kubeadmin password and no web-console login - authentication is via"
  echo "the client certificate embedded in the kubeconfig (user: system:admin)."
  echo ""
  echo "Requires: oc (logged into the Konflux cluster), base64"
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

# Look up the credential secret produced by a provision-ephemeral-cluster
# taskrun. The task exposes a "secretRef" result naming a secret that holds a
# "kubeconfig" key.
secret_for_role() {
  local role="$1"
  oc get taskrun -n "${NAMESPACE}" \
    -l "tekton.dev/pipelineRun=${PIPELINE_RUN}" -o json | \
    jq -r ".items[]
      | select(.metadata.name | test(\"provision-${role}\"))
      | .status.results[]? | select(.name == \"secretRef\") | .value" \
    | head -1
}

ORIGINAL_CTX=$(oc config current-context 2>/dev/null || true)

for role in hub managed; do
  CTX="eaas-${role}"
  SECRET=$(secret_for_role "${role}")

  if [[ -z "${SECRET}" ]]; then
    echo "ERROR: could not find provision-${role} secretRef for ${PIPELINE_RUN}."
    echo "The provision-${role} task may not have completed. Check with:"
    echo "  oc get taskrun -n ${NAMESPACE} -l tekton.dev/pipelineRun=${PIPELINE_RUN}"
    exit 1
  fi

  KCFG=$(mktemp)
  # shellcheck disable=SC2064
  trap "rm -f '${KCFG}'" EXIT
  oc get secret -n "${NAMESPACE}" "${SECRET}" \
    -o jsonpath='{.data.kubeconfig}' | base64 -d > "${KCFG}"

  if [[ ! -s "${KCFG}" ]]; then
    echo "ERROR: secret ${SECRET} has no kubeconfig data."
    exit 1
  fi

  # Pull cluster + client-cert credentials out of the provisioned kubeconfig.
  # We copy them under unique names because both provision kubeconfigs use the
  # same internal names (cluster/admin) and would otherwise collide on merge.
  SERVER=$(KUBECONFIG="${KCFG}" oc config view --raw \
    -o jsonpath='{.clusters[0].cluster.server}')
  CADATA=$(KUBECONFIG="${KCFG}" oc config view --raw \
    -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
  CERT=$(KUBECONFIG="${KCFG}" oc config view --raw \
    -o jsonpath='{.users[0].user.client-certificate-data}')
  KEY=$(KUBECONFIG="${KCFG}" oc config view --raw \
    -o jsonpath='{.users[0].user.client-key-data}')

  # Clean up any previous entries for this context.
  oc config delete-context "${CTX}" 2>/dev/null || true
  oc config delete-cluster "${CTX}" 2>/dev/null || true
  oc config delete-user "${CTX}-admin" 2>/dev/null || true

  oc config set-cluster "${CTX}" --server="${SERVER}" >/dev/null
  if [[ -n "${CADATA}" ]]; then
    oc config set "clusters.${CTX}.certificate-authority-data" "${CADATA}" >/dev/null
  else
    oc config set "clusters.${CTX}.insecure-skip-tls-verify" true >/dev/null
  fi
  oc config set-credentials "${CTX}-admin" >/dev/null
  oc config set "users.${CTX}-admin.client-certificate-data" "${CERT}" >/dev/null
  oc config set "users.${CTX}-admin.client-key-data" "${KEY}" >/dev/null
  oc config set-context "${CTX}" --cluster="${CTX}" --user="${CTX}-admin" >/dev/null

  rm -f "${KCFG}"
  trap - EXIT

  echo "  Created context ${CTX} -> ${SERVER}"
done

# Restore whatever context was active before.
if [[ -n "${ORIGINAL_CTX}" ]]; then
  oc config use-context "${ORIGINAL_CTX}" >/dev/null
fi

echo ""
for role in hub managed; do
  CTX="eaas-${role}"
  API=$(oc --context="${CTX}" whoami --show-server 2>/dev/null || echo "?")
  CONSOLE=$(oc --context="${CTX}" whoami --show-console 2>/dev/null || echo "?")
  USER=$(oc --context="${CTX}" whoami 2>/dev/null || echo "?")
  echo "=== ${role^^} CLUSTER ==="
  echo "  Context: ${CTX}"
  echo "  API:     ${API}"
  echo "  Console: ${CONSOLE}"
  echo "  User:    ${USER} (client-cert auth; no password / no web-console login)"
  echo ""
done

echo "Use with:"
echo "  oc --context=eaas-hub get nodes"
echo "  oc --context=eaas-managed get nodes"
echo ""
echo "Cancel the PipelineRun when done:"
echo "  oc patch pipelinerun ${PIPELINE_RUN} -n ${NAMESPACE} --type merge -p '{\"spec\":{\"status\":\"CancelledRunFinally\"}}'"

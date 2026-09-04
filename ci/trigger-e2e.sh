#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="obsint-processing-tenant"
COMPONENT="insights-on-prem"
ITS_NAME="insights-on-prem-eaas-e2e"

usage() {
  echo "Usage: $0 [options] [commit-sha]"
  echo ""
  echo "Finds the Konflux Snapshot built from the given commit and triggers"
  echo "the EaaS e2e test pipeline via the IntegrationTestScenario."
  echo ""
  echo "If no commit is provided, uses HEAD of the current git repo."
  echo ""
  echo "Options:"
  echo "  -f, --force            Retrigger (removes label first, then re-adds)"
  echo "  -d, --debug            Hold clusters on failure. Labels the PipelineRun"
  echo "                         with debug.iop/hold-on-failure=true so the finally"
  echo "                         task dumps credentials and sleeps on failure."
  echo "  -t, --test-filter STR  Label the PipelineRun with debug.iop/test-filter=STR"
  echo "                         so the test task reads it at runtime."
  echo "  -h, --help             Show this help"
  echo ""
  echo "Requires: oc (logged into the Konflux cluster), jq"
  exit 1
}

FORCE=false
DEBUG=false
TEST_FILTER=""

while [[ "${1:-}" == -* ]]; do
  case "$1" in
    -f|--force) FORCE=true; shift ;;
    -d|--debug) DEBUG=true; shift ;;
    -t|--test-filter)
      TEST_FILTER="${2:?--test-filter requires a value}"
      shift 2
      ;;
    -h|--help) usage ;;
    *) echo "Unknown option: $1"; usage ;;
  esac
done

COMMIT="${1:-$(git rev-parse HEAD)}"
SHORT_SHA="${COMMIT:0:7}"

echo "Looking for Snapshot matching commit ${SHORT_SHA} in ${NAMESPACE}..."

SNAPSHOT_NAME=$(oc get snapshot -n "${NAMESPACE}" \
  -l "appstudio.openshift.io/component=${COMPONENT}" \
  -o json | jq -r \
  ".items[] | select(.spec.components[]?.source.git.revision | test(\"^${COMMIT}\")) | .metadata.name" \
  | head -1)

if [[ -z "${SNAPSHOT_NAME}" ]]; then
  echo "ERROR: No Snapshot found for commit ${SHORT_SHA}."
  echo "The build may not have completed yet. Check with:"
  echo "  oc get snapshot -n ${NAMESPACE} -l appstudio.openshift.io/component=${COMPONENT}"
  exit 1
fi

IMAGE=$(oc get snapshot "${SNAPSHOT_NAME}" -n "${NAMESPACE}" \
  -o json | jq -r ".spec.components[] | select(.name == \"${COMPONENT}\") | .containerImage")

echo "Found Snapshot: ${SNAPSHOT_NAME}"
echo "Image:          ${IMAGE}"
echo ""

echo "Labeling Snapshot to trigger ${ITS_NAME}..."

if [[ "${FORCE}" == "true" ]]; then
  oc label "snapshot/${SNAPSHOT_NAME}" \
    "test.appstudio.openshift.io/run-" \
    -n "${NAMESPACE}" 2>/dev/null || true
fi

oc label "snapshot/${SNAPSHOT_NAME}" \
  "test.appstudio.openshift.io/run=${ITS_NAME}" \
  -n "${NAMESPACE}"

# If debug labels are needed, wait for the PipelineRun and label it
if [[ "${DEBUG}" == "true" || -n "${TEST_FILTER}" ]]; then
  echo ""
  echo "Waiting for PipelineRun to be created..."
  PIPELINE_RUN=""
  ELAPSED=0
  while [[ -z "${PIPELINE_RUN}" ]]; do
    if [[ "$ELAPSED" -ge 120 ]]; then
      echo "ERROR: PipelineRun not created after 2 minutes"
      exit 1
    fi
    sleep 5
    ELAPSED=$((ELAPSED + 5))
    PIPELINE_RUN=$(oc get pipelinerun -n "${NAMESPACE}" \
      -l "appstudio.openshift.io/snapshot=${SNAPSHOT_NAME},test.appstudio.openshift.io/scenario=${ITS_NAME}" \
      -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  done

  echo "PipelineRun: ${PIPELINE_RUN}"

  if [[ "${DEBUG}" == "true" ]]; then
    oc label "pipelinerun/${PIPELINE_RUN}" \
      "debug.iop/hold-on-failure=true" \
      -n "${NAMESPACE}"
    echo "  Labeled: debug.iop/hold-on-failure=true"
  fi

  if [[ -n "${TEST_FILTER}" ]]; then
    oc label "pipelinerun/${PIPELINE_RUN}" \
      "debug.iop/test-filter=${TEST_FILTER}" \
      -n "${NAMESPACE}"
    echo "  Labeled: debug.iop/test-filter=${TEST_FILTER}"
  fi

  echo ""
  echo "Monitor with:"
  echo "  oc get pipelinerun ${PIPELINE_RUN} -n ${NAMESPACE} --watch"
  if [[ "${DEBUG}" == "true" ]]; then
    echo ""
    echo "On failure, check logs for credentials:"
    echo "  oc logs -n ${NAMESPACE} ${PIPELINE_RUN}-hold-clusters-on-failure-pod print-credentials-and-wait"
    echo ""
    echo "When done debugging, cancel with:"
    echo "  oc patch pipelinerun ${PIPELINE_RUN} -n ${NAMESPACE} --type merge -p '{\"spec\":{\"status\":\"CancelledRunFinally\"}}'"
  fi
else
  echo "Done. Monitor the PipelineRun with:"
  echo "  oc get pipelinerun -n ${NAMESPACE} -l appstudio.openshift.io/snapshot=${SNAPSHOT_NAME} --watch"
fi

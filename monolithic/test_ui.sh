#!/bin/bash
# test_ui.sh - Sets up test data to verify all four Insights sections in the ACM fleet overview UI.
#
# Prerequisites: deploy.sh must have been run first (on-prem service + insights-client configured).
#
# Results are visible at: https://<your-cluster>/multicloud/home/overview

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
UI_TESTS="$SCRIPT_DIR/tests/ui"
CLUSTER_ID=$(oc get clusterversion version -o jsonpath='{.spec.clusterID}')

# Custom console image with UPGRADE_RISKS_PREDICTION_URL env var support baked in.
# Built from the original ACM console image with a one-line change - for testing only.
# See README "Custom console image for URP" section for details.
CONSOLE_IMAGE="quay.io/ccxdev/insights-on-prem-lsolarov-console:latest"

echo "=== Insights On-Premise UI Test Setup ==="
echo "Cluster ID: $CLUSTER_ID"
echo ""

# ---------------------------------------------------------------------------
echo "1. Triggering cluster recommendations..."
# ---------------------------------------------------------------------------
# Trigger 1: webhook_timeout_is_larger_than_default rule (insights-core / CCX)
# Creates a ValidatingWebhookConfiguration with timeoutSeconds > 13 for pod CREATE
# operations. insights-operator collects webhook configs as part of its archive and
# insights-core detects the misconfiguration. See webhook-trigger.yaml for details.
oc apply -f "$UI_TESTS/webhook-trigger.yaml"

# Trigger 2: operator_unmanaged rule — sets openshift-samples operator to Unmanaged.
# Safe to use as the samples operator is non-critical and it is reversible.
# Revert with: oc patch configs.samples.operator.openshift.io cluster --type merge -p '{"spec":{"managementState":"Managed"}}'
oc patch configs.samples.operator.openshift.io cluster --type merge -p '{"spec":{"managementState":"Unmanaged"}}'

# ---------------------------------------------------------------------------
echo ""
echo "2. Creating test data for update risk predictions and failing operators..."
# ---------------------------------------------------------------------------
oc apply -f "$UI_TESTS/critical-alerts.yaml"

# ---------------------------------------------------------------------------
echo ""
echo "3. Configuring on-prem service to query current Thanos data..."
# ---------------------------------------------------------------------------
# By default the on-prem service queries Thanos at (now - 60 minutes) as a point-in-time
# query, so freshly fired alerts wouldn't be visible. Setting to 0 queries the current
# timestamp so new alerts are picked up immediately. This does NOT cause constant Thanos
# requests — it only affects the timestamp used when /upgrade-risks-prediction is called.
oc set env deployment/insights-on-prem -n insights-on-prem-poc THANOS_QUERY_LOOKBACK_MINUTES=0
oc rollout status deployment/insights-on-prem -n insights-on-prem-poc --timeout=60s

# ---------------------------------------------------------------------------
echo ""
echo "4. Exposing on-prem service via HTTPS route (required for console backend)..."
# ---------------------------------------------------------------------------
# The ACM console backend enforces HTTPS for outbound calls, so the on-prem service
# must be reachable over HTTPS. This route is for testing only — in production the
# addon would handle service exposure properly.
oc create route edge insights-on-prem \
  -n insights-on-prem-poc \
  --service=insights-on-prem \
  --port=8000 \
  --insecure-policy=Redirect 2>/dev/null || true

ON_PREM_ROUTE=$(oc get route insights-on-prem -n insights-on-prem-poc -o jsonpath='{.spec.host}')
ON_PREM_URP_URL="https://${ON_PREM_ROUTE}/api/insights-results-aggregator/v2/upgrade-risks-prediction"
echo "   Route: $ON_PREM_URP_URL"

# ---------------------------------------------------------------------------
echo ""
echo "5. Deploying custom console image with UPGRADE_RISKS_PREDICTION_URL support..."
# ---------------------------------------------------------------------------
# The ACM console hardcodes console.redhat.com for URP calls. This custom image
# is built from the original console image with a one-line change that makes it
# read the URL from UPGRADE_RISKS_PREDICTION_URL env var instead — for testing only.
# Once the equivalent change lands in stolostron/console, this step reduces to
# just the oc set env below.
oc set image deployment/console-chart-console-v2 -n open-cluster-management \
  console=$CONSOLE_IMAGE
oc patch deployment console-chart-console-v2 -n open-cluster-management --type=strategic \
  -p='{"spec":{"template":{"spec":{"containers":[{"name":"console","imagePullPolicy":"Always"}]}}}}'
oc set env deployment/console-chart-console-v2 -n open-cluster-management \
  UPGRADE_RISKS_PREDICTION_URL=$ON_PREM_URP_URL
oc rollout status deployment/console-chart-console-v2 -n open-cluster-management --timeout=120s

# ---------------------------------------------------------------------------
echo ""
echo "6. Waiting for alerts to reach Thanos (~2-5 min)..."
# ---------------------------------------------------------------------------
TOKEN=$(oc exec deployment/insights-on-prem -n insights-on-prem-poc -- cat /var/run/secrets/kubernetes.io/serviceaccount/token)
for _ in $(seq 1 10); do
  COUNT=$(oc exec deployment/insights-on-prem -n insights-on-prem-poc -- sh -c \
    "curl -sk -H 'Authorization: Bearer $TOKEN' \
     'https://rbac-query-proxy.open-cluster-management-observability.svc.cluster.local:8443/api/v1/query' \
     --data-urlencode 'query=ALERTS{alertname=~\"InsightsTest.*\"}' 2>/dev/null" | \
    python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d['data']['result']))" 2>/dev/null)
  echo "   $(date '+%H:%M:%S') alerts in Thanos: ${COUNT:-0}"
  [ "${COUNT:-0}" -gt 0 ] && break
  sleep 30
done

# ---------------------------------------------------------------------------
echo ""
echo "7. Verifying URP data comes from on-prem via the actual console route..."
# ---------------------------------------------------------------------------
# Call ON_PREM_URP_URL (the HTTPS batch endpoint the console uses) with the
# same batch payload the console sends. This exercises the full path:
# console -> HTTPS route -> batch endpoint -> Thanos -> prediction.
# Calling localhost directly would bypass the route and miss regressions there.
URP_RESULT=$(curl -sk -X POST "$ON_PREM_URP_URL" \
  -H 'Content-Type: application/json' \
  -d "{\"clusters\": [\"$CLUSTER_ID\"]}" 2>/dev/null)

HAS_ALERTS=$(echo "$URP_RESULT" | grep -c "InsightsTestCriticalAlert" || true)
UPGRADE_RECOMMENDED=$(echo "$URP_RESULT" | python3 -c \
  "import sys,json; d=json.load(sys.stdin); p=d.get('predictions',[]); print(p[0].get('upgrade_recommended','?') if p else '?')" 2>/dev/null)

PASS=0; FAIL=0
check() {
  if [ "$2" = "ok" ]; then echo "  [PASS] $1"; PASS=$((PASS+1))
  else echo "  [FAIL] $1 — $2"; FAIL=$((FAIL+1)); fi
}

check "batch URP endpoint returns cluster-local fake alerts via HTTPS route (proves full path works)" \
  "$([ "${HAS_ALERTS:-0}" -gt 0 ] && echo ok || echo "alerts not found - Thanos may need more time")"
check "batch URP endpoint returns upgrade_recommended=False" \
  "$([ "$UPGRADE_RECOMMENDED" = "False" ] && echo ok || echo "got: $UPGRADE_RECOMMENDED")"

echo ""
echo "Results: $PASS passed, $FAIL failed"
echo ""
echo "=== Done ==="
echo "Check the UI at: https://$(oc get infrastructure cluster -o jsonpath='{.status.apiServerURL}' | sed 's|https://api\.|console-openshift-console.apps.|' | sed 's|:6443||')/multicloud/home/overview"
echo ""
echo "To clean up:"
echo "  oc delete validatingwebhookconfiguration insights-test-webhook"
echo "  oc patch configs.samples.operator.openshift.io cluster --type merge -p '{\"spec\":{\"managementState\":\"Managed\"}}'"
echo "  oc delete prometheusrule insights-test-alerts -n openshift-monitoring"
echo "  oc delete route insights-on-prem -n insights-on-prem-poc"

#!/bin/bash
# populate-sample-data.sh - Populates a cluster with sample data so you can validate
# that the Insights sections in the ACM fleet overview UI work correctly.
#
# Prerequisites: oc apply -f deploy/ must have been run first (on-prem service + insights-client configured).
#
# Results are visible at: https://<your-cluster>/multicloud/home/overview

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANIFESTS_DIR="$SCRIPT_DIR/tests/ui"
CLUSTER_ID=$(oc get clusterversion version -o jsonpath='{.spec.clusterID}')

echo "=== Insights On-Premise Sample Data Setup ==="
echo "Cluster ID: $CLUSTER_ID"
echo ""

# ---------------------------------------------------------------------------
echo "1. Triggering cluster recommendations..."
# ---------------------------------------------------------------------------
# Trigger 1: webhook_timeout_is_larger_than_default rule (insights-core / CCX)
# Creates a ValidatingWebhookConfiguration with timeoutSeconds > 13 for pod CREATE
# operations. insights-operator collects webhook configs as part of its archive and
# insights-core detects the misconfiguration. See webhook-trigger.yaml for details.
oc apply -f "$MANIFESTS_DIR/webhook-trigger.yaml"

# Trigger 2: operator_unmanaged rule — sets openshift-samples operator to Unmanaged.
# Safe to use as the samples operator is non-critical and it is reversible.
# Revert with: oc patch configs.samples.operator.openshift.io cluster --type merge -p '{"spec":{"managementState":"Managed"}}'
oc patch configs.samples.operator.openshift.io cluster --type merge -p '{"spec":{"managementState":"Unmanaged"}}'

# ---------------------------------------------------------------------------
echo ""
echo "2. Creating sample data for update risk predictions..."
# ---------------------------------------------------------------------------
oc apply -f "$MANIFESTS_DIR/critical-alerts.yaml"

# ---------------------------------------------------------------------------
echo ""
echo "3. Configuring on-prem service to query current Thanos data..."
# ---------------------------------------------------------------------------
# By default the on-prem service queries Thanos at (now - 60 minutes) as a point-in-time
# query, so freshly fired alerts wouldn't be visible. Setting to 0 queries the current
# timestamp so new alerts are picked up immediately. This does NOT cause constant Thanos
# requests — it only affects the timestamp used when /upgrade-risks-prediction is called.
oc set env deployment/insights-on-prem -n insights-on-prem THANOS_QUERY_LOOKBACK_MINUTES=0
oc rollout status deployment/insights-on-prem -n insights-on-prem --timeout=60s

# ---------------------------------------------------------------------------
echo ""
echo "4. Waiting for alerts to reach Thanos (~2-5 min)..."
# ---------------------------------------------------------------------------
for _ in $(seq 1 10); do
  # The token is read *inside* the pod ($(cat ...) runs in the pod because the
  # sh -c argument is single-quoted). This keeps the token out of the oc exec
  # argv, which would otherwise be recorded in the Kubernetes API audit log.
  COUNT=$(oc exec deployment/insights-on-prem -n insights-on-prem -- sh -c \
    'curl -sk -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" \
     "https://rbac-query-proxy.open-cluster-management-observability.svc.cluster.local:8443/api/v1/query" \
     --data-urlencode "query=ALERTS{alertname=~\"InsightsTest.*\"}" 2>/dev/null' | \
    python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d['data']['result']))" 2>/dev/null)
  echo "   $(date '+%H:%M:%S') alerts in Thanos: ${COUNT:-0}"
  [ "${COUNT:-0}" -gt 0 ] && break
  sleep 30
done

# ---------------------------------------------------------------------------
echo ""
echo "5. Verifying URP data via HAProxy proxy..."
# ---------------------------------------------------------------------------
URP_RESULT=$(oc exec deployment/insights-on-prem-proxy -n insights-on-prem -c haproxy -- \
  curl -sk -X POST "https://localhost:8443/api/insights-results-aggregator/v2/upgrade-risks-prediction" \
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

check "URP endpoint returns sample alerts via HAProxy" \
  "$([ "${HAS_ALERTS:-0}" -gt 0 ] && echo ok || echo "alerts not found - Thanos may need more time")"
check "URP endpoint returns upgrade_recommended=False" \
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
echo "  oc set env deployment/insights-on-prem -n insights-on-prem THANOS_QUERY_LOOKBACK_MINUTES-"

[ "$FAIL" -eq 0 ] || exit 1

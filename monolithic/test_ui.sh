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

echo "=== Insights On-Premise UI Test Setup ==="
echo "Cluster ID: $CLUSTER_ID"
echo ""

# ---------------------------------------------------------------------------
echo "1. Triggering cluster recommendations (webhook rule)..."
# ---------------------------------------------------------------------------
oc apply -f "$UI_TESTS/webhook-trigger.yaml"

# ---------------------------------------------------------------------------
echo ""
echo "2. Creating test data for update risk predictions and failing operators..."
# ---------------------------------------------------------------------------
oc apply -f "$UI_TESTS/degraded-operator.yaml"
oc patch clusteroperator insights-test-operator --type=merge --subresource=status -p '{
  "status": {
    "conditions": [
      {"type": "Degraded",    "status": "True",  "reason": "TestDegradation", "message": "Test degradation for URP UI testing", "lastTransitionTime": "2024-01-01T00:00:00Z"},
      {"type": "Available",   "status": "True",  "reason": "AsExpected",      "lastTransitionTime": "2024-01-01T00:00:00Z"},
      {"type": "Progressing", "status": "False", "reason": "AsExpected",      "lastTransitionTime": "2024-01-01T00:00:00Z"}
    ]
  }
}'
oc apply -f "$UI_TESTS/critical-alerts.yaml"

# ---------------------------------------------------------------------------
echo ""
echo "3. Configuring on-prem service to query current Thanos data..."
# ---------------------------------------------------------------------------
oc set env deployment/insights-on-prem -n insights-on-prem-poc THANOS_QUERY_LOOKBACK_MINUTES=0

# ---------------------------------------------------------------------------
echo ""
echo "4. Patching ACM console backend to redirect URP calls to on-prem service..."
# ---------------------------------------------------------------------------
CONSOLE_POD=$(oc get pod -n open-cluster-management -l component=console -o jsonpath='{.items[0].metadata.name}')
oc cp open-cluster-management/${CONSOLE_POD}:/app/backend.mjs /tmp/backend.mjs -c console
python3 "$UI_TESTS/patch-console-urp.py"

oc delete configmap console-backend-patched -n open-cluster-management --ignore-not-found
oc create configmap console-backend-patched -n open-cluster-management --from-file=backend.mjs=/tmp/backend.mjs
oc patch deployment console-chart-console-v2 -n open-cluster-management --type=json -p='[
  {"op":"add","path":"/spec/template/spec/volumes/-","value":{"name":"backend-patch","configMap":{"name":"console-backend-patched"}}},
  {"op":"add","path":"/spec/template/spec/containers/0/volumeMounts/-","value":{"name":"backend-patch","mountPath":"/app/backend.mjs","subPath":"backend.mjs"}}
]' 2>/dev/null || true
oc rollout restart deployment/console-chart-console-v2 -n open-cluster-management
oc rollout status deployment/console-chart-console-v2 -n open-cluster-management --timeout=120s

# ---------------------------------------------------------------------------
echo ""
echo "5. Waiting for alerts to reach Thanos (~1-2 min)..."
# ---------------------------------------------------------------------------
TOKEN=$(oc exec deployment/insights-on-prem -n insights-on-prem-poc -- cat /var/run/secrets/kubernetes.io/serviceaccount/token)
for i in $(seq 1 10); do
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
echo "6. Verifying URP data comes from on-prem (not console.redhat.com)..."
# ---------------------------------------------------------------------------
URP_RESULT=$(oc exec deployment/insights-on-prem -n insights-on-prem-poc -- sh -c \
  "curl -s -X POST http://localhost:8000/upgrade-risks-prediction \
   -H 'Content-Type: application/json' \
   -d '{\"cluster_id\": \"$CLUSTER_ID\"}'" 2>/dev/null)

HAS_ALERTS=$(echo "$URP_RESULT" | grep -c "InsightsTestCriticalAlert" || true)
HAS_OPERATOR=$(echo "$URP_RESULT" | grep -c "insights-test-operator" || true)
UPGRADE_RECOMMENDED=$(echo "$URP_RESULT" | python3 -c \
  "import sys,json; print(json.load(sys.stdin).get('upgrade_recommended','?'))" 2>/dev/null)

PASS=0; FAIL=0
check() {
  if [ "$2" = "ok" ]; then echo "  [PASS] $1"; PASS=$((PASS+1))
  else echo "  [FAIL] $1 — $2"; FAIL=$((FAIL+1)); fi
}

check "on-prem URP returns cluster-local fake alerts (proves data not from console.redhat.com)" \
  "$([ "${HAS_ALERTS:-0}" -gt 0 ] && echo ok || echo "alerts not found - Thanos may need more time")"
check "on-prem URP returns degraded operator condition" \
  "$([ "${HAS_OPERATOR:-0}" -gt 0 ] && echo ok || echo "operator not found")"
check "on-prem URP returns upgrade_recommended=False" \
  "$([ "$UPGRADE_RECOMMENDED" = "False" ] && echo ok || echo "got: $UPGRADE_RECOMMENDED")"

echo ""
echo "Results: $PASS passed, $FAIL failed"
echo ""
echo "=== Done ==="
echo "Check the UI at: https://$(oc get infrastructure cluster -o jsonpath='{.status.apiServerURL}' | sed 's|https://api\.|console-openshift-console.apps.|' | sed 's|:6443||')/multicloud/home/overview"
echo ""
echo "To clean up:"
echo "  oc delete validatingwebhookconfiguration insights-test-webhook"
echo "  oc delete clusteroperator insights-test-operator"
echo "  oc delete prometheusrule insights-test-alerts -n openshift-monitoring"
echo "  oc delete configmap console-backend-patched -n open-cluster-management"

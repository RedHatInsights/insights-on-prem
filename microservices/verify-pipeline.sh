#!/bin/bash
# EDP Pipeline Verification Script

set -e

echo "=== Pipeline Verification ==="

# Check insights-operator upload (check IO logs first, fall back to ingress if logs rotated)
echo -n "Insights upload: "
if oc logs -n openshift-insights -l app=insights-operator --since=10m 2>/dev/null | grep -q "Uploaded report successfully"; then
    echo "✓"
else
    echo "⚠️  (wait ~2min after: oc delete pod -n openshift-insights -l app=insights-operator)"
fi

# Check ingress received payload
echo -n "Ingress received: "
if oc logs -n edp-processing deployment/ingress --since=10m 2>/dev/null | grep -q "Payload received"; then
    ORG=$(oc logs -n edp-processing deployment/ingress --since=10m 2>/dev/null | grep "Payload received" | tail -1 | grep -o '"org_id":"[^"]*"' | cut -d'"' -f4)
    echo "✓ (org_id: $ORG)"
else
    echo "❌"
fi

# Check processing
echo -n "Archive processed: "
if oc logs -n edp-processing deployment/db-writer --since=10m 2>/dev/null | grep -q "Stored info report"; then
    CLUSTER=$(oc logs -n edp-processing deployment/db-writer --since=10m 2>/dev/null | grep "Stored info report" | tail -1 | grep -o '"cluster":"[^"]*"' | cut -d'"' -f4)
    echo "✓ (cluster: $CLUSTER)"
else
    echo "❌"
fi

# Check ACM client (optional)
if oc get deployment insights-client -n open-cluster-management &>/dev/null; then
    echo -n "ACM client query: "
    if oc logs -n open-cluster-management deployment/insights-client --since=30m 2>/dev/null | grep -q "identity-injector.edp-processing"; then
        echo "✓"
    else
        echo "⚠️  (not configured)"
    fi
fi

# Measure end-to-end latency from database timestamps
echo -n "Processing latency: "
LATENCY_DATA=$(oc exec -n edp-processing postgresql-0 -- bash -c "PGPASSWORD=password psql -U user -d aggregator -t -c \"SELECT last_checked_at, reported_at, EXTRACT(EPOCH FROM (reported_at - last_checked_at)) FROM report ORDER BY last_checked_at DESC LIMIT 1;\"" 2>/dev/null)
if [ -n "$LATENCY_DATA" ]; then
    GATHERED=$(echo "$LATENCY_DATA" | awk '{print $1, $2}')
    STORED=$(echo "$LATENCY_DATA" | awk '{print $4, $5}')
    LATENCY=$(echo "$LATENCY_DATA" | awk '{print $7}')
    echo "${LATENCY}s (gathered: $GATHERED, stored: $STORED)"
else
    echo "N/A"
fi

echo
echo "Pipeline: insights-operator → identity-injector → ingress → Kafka → ccx-data-pipeline → db-writer"

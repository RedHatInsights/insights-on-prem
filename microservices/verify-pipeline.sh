#!/bin/bash
# EDP Pipeline Verification Script

set -e

# Parse verbose flag
VERBOSE=false
if [[ "$1" == "-v" || "$1" == "--verbose" ]]; then
    VERBOSE=true
fi

echo "=== Pipeline Verification ==="
echo

# Check insights-operator upload
echo -n "Insights upload: "
if $VERBOSE; then
    echo
    echo "  Command: oc logs -n openshift-insights deployment/insights-operator --since=10m | grep 'Uploaded report successfully'"
fi
UPLOAD_LOG=$(oc logs -n openshift-insights deployment/insights-operator --since=10m 2>/dev/null | grep "Uploaded report successfully" || true)
if [ -n "$UPLOAD_LOG" ]; then
    echo "✓"
    if $VERBOSE; then
        echo "  Found: $UPLOAD_LOG"
        echo
    fi
else
    echo "⚠️  (wait ~2min after: oc delete pod -n openshift-insights -l app=insights-operator)"
    $VERBOSE && echo
fi

# Check ingress received payload
echo -n "Ingress received: "
if $VERBOSE; then
    echo
    echo "  Command: oc logs -n edp-processing deployment/ingress --since=10m | grep 'Payload received'"
fi
INGRESS_LOG=$(oc logs -n edp-processing deployment/ingress --since=10m 2>/dev/null | grep "Payload received" | tail -1 || true)
if [ -n "$INGRESS_LOG" ]; then
    ORG=$(echo "$INGRESS_LOG" | grep -o '"org_id":"[^"]*"' | cut -d'"' -f4)
    echo "✓ (org_id: $ORG)"
    if $VERBOSE; then
        echo "  Found: $INGRESS_LOG"
        echo
    fi
else
    echo "❌"
    $VERBOSE && echo
fi

# Check processing
echo -n "Archive processed: "
if $VERBOSE; then
    echo
    echo "  Command: oc logs -n edp-processing deployment/db-writer --since=10m | grep 'Stored info report'"
fi
DB_LOG=$(oc logs -n edp-processing deployment/db-writer --since=10m 2>/dev/null | grep "Stored info report" | tail -1 || true)
if [ -n "$DB_LOG" ]; then
    CLUSTER=$(echo "$DB_LOG" | grep -o '"cluster":"[^"]*"' | cut -d'"' -f4)
    echo "✓ (cluster: $CLUSTER)"
    if $VERBOSE; then
        echo "  Found: $DB_LOG"
        echo
    fi
else
    echo "❌"
    $VERBOSE && echo
fi

# Check ACM client (optional)
if oc get deployment insights-client -n open-cluster-management &>/dev/null; then
    echo -n "ACM client query: "
    if $VERBOSE; then
        echo
        echo "  Command: oc logs -n open-cluster-management deployment/insights-client --since=30m | grep 'identity-injector.edp-processing'"
    fi
    ACM_LOG=$(oc logs -n open-cluster-management deployment/insights-client --since=30m 2>/dev/null | grep "identity-injector.edp-processing" | tail -1 || true)
    if [ -n "$ACM_LOG" ]; then
        echo "✓"
        if $VERBOSE; then
            echo "  Found: $ACM_LOG"
            echo
        fi
    else
        echo "⚠️  (not configured)"
        $VERBOSE && echo
    fi

    # Check PolicyReport created by ACM
    echo -n "ACM PolicyReport: "
    if $VERBOSE; then
        echo
        echo "  Command: oc get policyreport -n local-cluster local-cluster-policyreport"
    fi
    if oc get policyreport -n local-cluster local-cluster-policyreport &>/dev/null; then
        POLICY_COUNT=$(oc get policyreport -n local-cluster local-cluster-policyreport -o jsonpath='{.results}' 2>/dev/null | grep -o '"result":"[^"]*"' | wc -l | tr -d ' ')
        echo "✓ (${POLICY_COUNT} results)"
        if $VERBOSE; then
            echo "  Run: oc describe policyreport -n local-cluster local-cluster-policyreport"
            echo
        fi
    else
        echo "⚠️  (not created yet)"
        $VERBOSE && echo
    fi
fi

# Measure end-to-end latency from database timestamps
echo -n "Processing latency: "
CMD="oc exec -n edp-processing postgresql-0 -- bash -c \"PGPASSWORD=password psql -U user -d aggregator -t -c 'SELECT last_checked_at, reported_at, EXTRACT(EPOCH FROM (reported_at - last_checked_at)) FROM report ORDER BY last_checked_at DESC LIMIT 1;'\""
if $VERBOSE; then
    echo
    echo "  Command: $CMD"
fi
LATENCY_DATA=$(eval "$CMD" 2>/dev/null || true)
if [ -n "$LATENCY_DATA" ]; then
    GATHERED=$(echo "$LATENCY_DATA" | awk '{print $1, $2}')
    STORED=$(echo "$LATENCY_DATA" | awk '{print $4, $5}')
    LATENCY=$(echo "$LATENCY_DATA" | awk '{print $7}')
    echo "${LATENCY}s (gathered: $GATHERED, stored: $STORED)"
    if $VERBOSE; then
        echo "  Result: $LATENCY_DATA"
        echo
    fi
else
    echo "N/A"
    $VERBOSE && echo
fi

echo
echo "Pipeline: insights-operator → identity-injector → ingress → Kafka → ccx-data-pipeline → db-writer"
if ! $VERBOSE; then
    echo
    echo "Tip: Run with -v or --verbose for detailed command output"
fi

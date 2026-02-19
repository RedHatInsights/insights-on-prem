#!/bin/bash
set -e

echo "=== Deploying Insights On-Premise POC ==="

# Deploy the on-premise service
echo "1. Creating namespace..."
oc apply -f deploy/namespace.yml

echo "2. Copying PostgreSQL secret..."
oc get secret search-postgres -n open-cluster-management -o json | \
  jq 'del(.metadata.namespace, .metadata.uid, .metadata.resourceVersion, .metadata.creationTimestamp, .metadata.ownerReferences) | .metadata.namespace = "insights-on-prem-poc"' | \
  oc apply --namespace insights-on-prem-poc -f -

echo "3. Applying secrets..."
oc apply -f deploy/ccxdev-insights-on-prem-poc-secret.yml --namespace insights-on-prem-poc

echo "4. Deploying application..."
oc apply -f deploy/insights.yml --namespace insights-on-prem-poc

echo "5. Creating service..."
oc apply -f deploy/service.yml --namespace insights-on-prem-poc

echo "6. Configuring OpenShift insights-operator..."
# Apply insights-operator ConfigMap to redirect uploads to on-premise service
oc apply -f deploy/insights-config.yml
oc rollout restart -n openshift-insights deployment insights-operator

echo "7. Pausing MultiClusterHub operator..."
# Pause the operator to prevent it from reverting our changes in insights-client deployment
oc annotate multiclusterhub multiclusterhub -n open-cluster-management mch-pause=true --overwrite

echo "8. Configuring ACM insights-client..."
# Update the CCX_SERVER environment variable to point to on-premise service
oc set env deployment/insights-client -n open-cluster-management \
  CCX_SERVER=http://insights-on-prem.insights-on-prem-poc.svc.cluster.local:8000/api/v2

# Set insights-client poll interval to 1 minute for demo purposes
oc set env deployment/insights-client -n open-cluster-management \
  POLL_INTERVAL=1

echo "9. Waiting for deployment to roll out..."
oc rollout status deployment/insights-client -n open-cluster-management --timeout=120s

echo ""
echo "=== Deployment Complete ==="
echo ""
echo "IMPORTANT: MultiClusterHub operator is PAUSED (mch-pause=true annotation)"
echo "           This prevents the operator from reverting the CCX_SERVER change."
echo "           To unpause: oc annotate multiclusterhub multiclusterhub -n open-cluster-management mch-pause-"
echo ""
echo "To verify insights-client configuration:"
echo "  oc get deployment insights-client -n open-cluster-management -o yaml | grep -A2 'name: CCX_SERVER'"

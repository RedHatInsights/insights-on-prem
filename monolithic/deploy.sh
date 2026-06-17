#!/bin/bash
set -e

echo "=== Deploying Insights On-Premise POC ==="

# Deploy the on-premise service
echo "1. Creating namespace..."
oc apply -f deploy/namespace.yml

echo "2. Deploying PostgreSQL..."
oc apply -f deploy/postgres.yml --namespace insights-on-prem-poc

echo "3. Applying secrets..."
oc apply -f deploy/ccxdev-insights-on-prem-poc-secret.yml --namespace insights-on-prem-poc

echo "4. Setting up ServiceAccount for Thanos access..."
oc apply -f deploy/serviceaccount.yml

echo "5. Creating service..."
oc apply -f deploy/service.yml --namespace insights-on-prem-poc

echo "6. Deploying application..."
oc apply -f deploy/insights.yml --namespace insights-on-prem-poc

echo "7. Creating Route for spoke access..."
oc apply -f deploy/route.yml --namespace insights-on-prem-poc

echo "8. Creating Placement for managed clusters..."
oc apply -f deploy/placement.yml

echo "9. Deploying OCM addon template..."
oc apply -f deploy/addon-template.yml

echo "10. Deploying OCM addon deployment config..."
oc apply -f deploy/addon-deployment-config.yml

echo "11. Deploying OCM addon registration..."
oc apply -f deploy/addon-registration.yml

echo "12. Deploying spoke policy (proxy manifests via hub templates)..."
oc apply -f deploy/spoke-policy.yml

echo "13. Pausing MultiClusterHub operator..."
# Pause the operator to prevent it from reverting our changes in insights-client deployment
oc annotate multiclusterhub multiclusterhub -n open-cluster-management mch-pause=true --overwrite

echo "14. Configuring ACM insights-client..."
# Update the CCX_SERVER environment variable to point to on-premise service
# Also, set insights-client poll interval to 1 minute for demo purposes
oc set env deployment/insights-client -n open-cluster-management \
  CCX_SERVER=http://insights-on-prem.insights-on-prem-poc.svc.cluster.local:8000/api/v2 \
  POLL_INTERVAL=1

echo "15. Waiting for insights-client to roll out..."
oc rollout status deployment/insights-client -n open-cluster-management --timeout=120s

echo "16. Configuring ACM console for upgrade risk predictions..."
# The ACM console hardcodes console.redhat.com for URP — deploy a custom image that
# reads UPGRADE_RISKS_PREDICTION_URL env var instead (see README for details).
# Must be done AFTER pausing MCH (step 13), otherwise MCH reverts the image.
# Reuse the existing pull secret (same ccxdev+insights_on_prem_poc robot account).
# Copy it to open-cluster-management so the console deployment can pull the image.
oc get secret ccxdev-insights-on-prem-poc-pull-secret -n insights-on-prem-poc -o json | \
  python3 -c "import sys,json; d=json.load(sys.stdin); d['metadata']={'name':'ccxdev-insights-on-prem-poc-pull-secret','namespace':'open-cluster-management'}; print(json.dumps(d))" | \
  oc apply -f -
oc set image deployment/console-chart-console-v2 -n open-cluster-management \
  console=quay.io/ccxdev/insights-on-prem-lsolarov-console:latest
# Strategic merge patch appends to imagePullSecrets by name rather than replacing the list.
oc patch deployment console-chart-console-v2 -n open-cluster-management --type=strategic \
  -p='{"spec":{"template":{"spec":{"imagePullSecrets":[{"name":"ccxdev-insights-on-prem-poc-pull-secret"}],"containers":[{"name":"console","imagePullPolicy":"Always"}]}}}}'
# UPGRADE_RISKS_PREDICTION_URL is set by test_ui.sh after the route is created
oc rollout status deployment/console-chart-console-v2 -n open-cluster-management --timeout=120s

echo ""
echo "=== Deployment Complete ==="
echo ""
echo "IMPORTANT: MultiClusterHub operator is PAUSED (mch-pause=true annotation)"
echo "           This prevents MCH from reverting CCX_SERVER and the console image."
echo "           If you unpause MCH, re-run deploy.sh to restore these changes."
echo "           To unpause: oc annotate multiclusterhub multiclusterhub -n open-cluster-management mch-pause-"
echo ""
echo "Next: run test_ui.sh to set up test data and configure URP routing."

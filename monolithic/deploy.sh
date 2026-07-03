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

echo "6. Creating Route for spoke access..."
oc apply -f deploy/route.yml --namespace insights-on-prem-poc
oc wait --for=jsonpath='{.spec.host}' route/insights-on-prem -n insights-on-prem-poc --timeout=30s

echo "7. Creating Placement for managed clusters..."
oc apply -f deploy/placement.yml

echo "8. Setting up cert-manager and certificates..."
# Install cert-manager operator and hub-side Policy for certificate management.
# The Policy creates CAs, issuers, and server cert once cert-manager is ready.
oc apply -f deploy/cert-manager.yml
echo "    Waiting for cert-manager..."
oc wait namespace/cert-manager --for=jsonpath='{.status.phase}=Active' --timeout=300s
oc wait deployment/cert-manager -n cert-manager --for=condition=Available --timeout=300s

# The pod will stay in ContainerCreating until cert-manager issues the server
# certificate and the Secret volumes become available. This is expected.
echo "9. Deploying application..."
oc apply -f deploy/insights.yml --namespace insights-on-prem-poc

echo "10. Deploying OCM addon template..."
oc apply -f deploy/addon-template.yml

echo "11. Deploying OCM addon deployment config..."
oc apply -f deploy/addon-deployment-config.yml

echo "12. Deploying OCM addon registration..."
oc apply -f deploy/addon-registration.yml

echo "13. Deploying spoke policy (proxy manifests via hub templates)..."
oc apply -f deploy/spoke-policy.yml

echo "    Waiting for service CA bundle..."
oc wait configmap/insights-on-prem-service-ca -n open-cluster-management --for=jsonpath='{.data.service-ca\.crt}' --timeout=300s

echo "14. Pausing MultiClusterHub operator..."
# Pause the operator to prevent it from reverting our changes in insights-client deployment
oc annotate multiclusterhub multiclusterhub -n open-cluster-management mch-pause=true --overwrite

echo "15. Configuring ACM insights-client..."
# insights-client goes through the spoke proxy, same as on managed clusters.
# The proxy uses a service-serving cert; SSL_CERT_DIR adds the service CA
# on top of the system trust bundle so insights-client can verify it.
oc set env deployment/insights-client -n open-cluster-management \
  CCX_SERVER=https://insights-operator-proxy.openshift-insights.svc.cluster.local:8443/api/v2 \
  SSL_CERT_DIR=/service-ca \
  POLL_INTERVAL=1
oc set volume deployment/insights-client -n open-cluster-management \
  --add --overwrite \
  --name=service-ca \
  --type=configmap \
  --configmap-name=insights-on-prem-service-ca \
  --mount-path=/service-ca \
  --read-only=true

echo "16. Waiting for insights-client to roll out..."
oc rollout status deployment/insights-client -n open-cluster-management --timeout=120s

echo "17. Pointing ACM console URP to the on-prem service..."
oc set env deployment/console-chart-console-v2 -n open-cluster-management \
  UPGRADE_RISKS_PREDICTION_URL=https://insights-operator-proxy.openshift-insights.svc:8443/api/insights-results-aggregator/v2/upgrade-risks-prediction \
  NODE_EXTRA_CA_CERTS=/service-ca/service-ca.crt
oc set volume deployment/console-chart-console-v2 -n open-cluster-management \
  --add --overwrite \
  --name=service-ca \
  --type=configmap \
  --configmap-name=insights-on-prem-service-ca \
  --mount-path=/service-ca \
  --read-only=true
oc rollout status deployment/console-chart-console-v2 -n open-cluster-management --timeout=120s

echo ""
echo "=== Deployment Complete ==="
echo ""
echo "IMPORTANT: MultiClusterHub operator is PAUSED (mch-pause=true annotation)"
echo "           This prevents MCH from reverting CCX_SERVER and UPGRADE_RISKS_PREDICTION_URL."
echo "           If you unpause MCH, re-run deploy.sh to restore these changes."
echo "           To unpause: oc annotate multiclusterhub multiclusterhub -n open-cluster-management mch-pause-"
echo ""
echo "Next: run test_ui.sh to set up test data and verify URP routing."

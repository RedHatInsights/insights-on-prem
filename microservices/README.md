# External Data Pipeline (EDP) - Direct Deployment

## Overview

This is a streamlined deployment of the EDP stack directly to an OpenShift cluster, without requiring ACM (Advanced Cluster Management).

The EDP consists of:

### Infrastructure Services
- **PostgreSQL**: Two instances for aggregator and notification databases
- **Kafka**: Message broker for data pipeline communication (Strimzi operator)
- **Redis**: Cache for aggregator results
- **MinIO**: S3-compatible storage for uploaded archives
- **Mock OAuth2 Server**: For development/testing authentication
- **RHOBS Mock**: For development/testing observability API
- **Identity Injector**: Nginx proxy that adds x-rh-identity header (acts like 3scale for on-prem)

### Processing Services
- **ingress**: HTTP endpoint for receiving archive uploads (port 3000)
- **ccx-data-pipeline**: Processes incoming cluster telemetry data
- **dvo-extractor**: Extracts DVO (Deployment Validation Operator) insights
- **db-writer**: Writes OCP recommendations to PostgreSQL
- **dvo-writer**: Writes DVO recommendations to PostgreSQL
- **cache-writer**: Writes results to Redis cache
- **aggregator**: REST API for querying stored recommendations (port 8082)
- **smart-proxy**: Unified API that aggregates multiple backend services (port 8080)
- **content-service**: Provides recommendation content and metadata (port 8081)
- **ccx-upgrades-data-eng**: Upgrade risk prediction data engineering service
- **ccx-upgrades-inference**: ML inference service for upgrade risks
- **notification-writer**: Writes notification events to database

Total: **20 pods**

## Architecture

![EDP Architecture](docs/on_prem_done.png)

The on-prem deployment consists of two main data paths:

**Upload Path** (insights-operator → identity-injector → ingress → Kafka → processing):
1. insights-operator uploads cluster archives to identity-injector
2. Identity-injector adds x-rh-identity header with org_id=000001 and forwards to ingress
3. Ingress stores archive in MinIO and publishes to Kafka
4. ccx-data-pipeline processes the archive and extracts recommendations
5. Writers store results in PostgreSQL and Redis

**Query Path** (insights-client → identity-injector → smart-proxy → aggregator):
1. insights-client requests reports from identity-injector
2. Identity-injector adds x-rh-identity header with org_id=000001 and forwards to smart-proxy
3. Smart-proxy validates identity and forwards to aggregator
4. Aggregator queries PostgreSQL and returns results

The identity-injector acts like 3scale in production - it adds authentication headers for on-prem deployments where clients don't send x-rh-identity.

## Quick Start

In Cluster Bot: 
```bash
launch 4.20 aws,large
```

Once the cluster is ready:

```bash
# Log in into the cluster (server URL in kubeconfig)
oc login -u kubeadmin -p <password> <URL>

# Get the cluster URL or simply use the one Clusterbot gave
oc whoami --show-console
```

## Automated Setup (Recommended)

Use the provided `edp.sh` script for a streamlined installation:

```bash
# Setup Quay credentials first (one-time)
oc create ns edp-processing
oc create secret docker-registry quay-pull-secret \
  --docker-server=quay.io \
  --docker-username=<your-quay-username> \
  --docker-password=<your-quay-password> \
  -n edp-processing
oc secrets link default quay-pull-secret --for=pull -n edp-processing

# Complete automated setup (all components)
./edp.sh all

# Or run components individually:
./edp.sh kafka        # Step 1: Kafka cluster
./edp.sh databases    # Step 2: PostgreSQL, Redis, MinIO
./edp.sh services     # Step 3: Processing services and identity-injector
./edp.sh routes       # Step 4: Create routes
./edp.sh insights     # Step 5: Configure insights-operator

# Optional: Configure ACM insights-client (requires ACM)
./edp.sh acm-client

# Verify deployment
./edp.sh verify

# Verify pipeline is processing archives
./verify-pipeline.sh

# Clean up everything
./edp.sh cleanup
```

**Available Commands:**
- `./edp.sh all` - Complete automated setup (runs all steps)
- `./edp.sh kafka` - Install Strimzi operator and deploy Kafka cluster
- `./edp.sh databases` - Deploy PostgreSQL, Redis, MinIO, and mocks
- `./edp.sh services` - Deploy all EDP processing services and identity-injector
- `./edp.sh routes` - Create OpenShift routes for services
- `./edp.sh insights` - Configure insights-operator to use local EDP
- `./edp.sh acm-client` - Configure ACM insights-client (optional, requires ACM)
- `./edp.sh verify` - Health check (verify all components are running)
- `./verify-pipeline.sh` - Verify archive upload and processing
- `./edp.sh restart` - Restart infrastructure pods
- `./edp.sh cleanup` - Remove all EDP components

## Manual Setup (Alternative)

If you prefer to deploy step-by-step manually:

### Step 1: Install Strimzi Operator

```bash
# Create kafka namespace
oc create namespace kafka

# Install Strimzi operator
oc create -f 'https://strimzi.io/install/latest?namespace=kafka' -n kafka

# Verify operator is running
oc get pods -n kafka
# Wait for: strimzi-cluster-operator-xxxxx   1/1   Running
```

### Step 2: Deploy Kafka Cluster

```bash
# Apply Kafka cluster and topics
oc apply -f deploy/03-kafka-strimzi.yaml

# Wait for Kafka to be ready (takes 2-3 minutes)
oc wait kafka/edp-kafka --for=condition=Ready --timeout=300s -n kafka

# Verify Kafka is running
oc get kafka -n kafka
oc get kafkatopic -n kafka
```

### Step 3: Deploy EDP Stack

```bash
# Create namespace
oc apply -f deploy/00-namespace.yaml

# Configure image pull credentials - testing version
oc create secret docker-registry quay-pull-secret \
  --docker-server=quay.io \
  --docker-username=<your-quay-username> \
  --docker-password=<your-quay-password> \
  -n edp-processing

# Link the secret to the default service account
oc secrets link default quay-pull-secret --for=pull -n edp-processing

# Create secrets
oc apply -f deploy/01-secrets.yaml

# Deploy infrastructure (PostgreSQL, Redis, MinIO, mocks)
oc apply -f deploy/02-infrastructure.yaml

# Wait for infrastructure to be ready
oc wait --for=condition=ready pod -l app=postgresql -n edp-processing --timeout=300s
oc wait --for=condition=ready pod -l app=minio -n edp-processing --timeout=300s

# Deploy application services
oc apply -f deploy/04-ingestion.yaml
oc apply -f deploy/05-writers.yaml
oc apply -f deploy/06-api-services.yaml
oc apply -f deploy/07-upgrades.yaml

# Wait for services to be ready
oc wait --for=condition=ready pod -l app=ingress -n edp-processing --timeout=300s
oc wait --for=condition=ready pod -l app=smart-proxy -n edp-processing --timeout=300s

# Deploy identity-injector (requires smart-proxy to exist first)
oc apply -f deploy/08-identity-injector.yaml

# Wait for identity-injector to be ready
oc wait --for=condition=ready pod -l app=identity-injector -n edp-processing --timeout=300s
```

### Step 4: Verify Deployment

```bash
# Check all pods are running
oc get pods -n edp-processing

# Check Kafka
oc get pods -n kafka

# You should see all 20 pods in Running state
```

Expected output:
```
> oc get pods -n edp-processing

NAME                                      READY   STATUS      RESTARTS   AGE
aggregator-6494dc7976-5csd7               1/1     Running     0          104s
cache-writer-5f4c47497b-ksp55             1/1     Running     0          104s
ccx-data-pipeline-b465d9bf-m49t4          1/1     Running     0          105s
ccx-upgrades-data-eng-7685565f99-88rnx    1/1     Running     0          102s
ccx-upgrades-inference-6f86d86854-rcg6r   1/1     Running     0          101s
content-service-67cd5f4889-2ccdf          1/1     Running     0          103s
db-writer-77dd578f6d-qcgv5                1/1     Running     0          105s
dvo-extractor-558bbf6ddc-scvdk            1/1     Running     0          105s
dvo-writer-55994d9b5-bclj2                1/1     Running     0          104s
identity-injector-59bd48b649-ln2sm        1/1     Running     0          90s
ingress-67f47c644-9qkjb                   1/1     Running     0          100s
minio-0                                   1/1     Running     0          2m23s
minio-create-buckets-cc5xg                0/1     Completed   0          2m22s
mock-oauth2-server-5bd8bd579-pmbrk        1/1     Running     0          2m24s
postgresql-0                              1/1     Running     0          2m26s
redis-5f6b544485-wmzbj                    1/1     Running     0          2m25s
rhobs-mock-85895c697b-ct5b7               1/1     Running     0          2m23s
smart-proxy-58f8ddffb9-xxwkl              1/1     Running     0          102s

> oc get pods -n kafka

NAME                                         READY   STATUS    RESTARTS   AGE
edp-kafka-dual-role-0                        1/1     Running   0          4m53s
edp-kafka-entity-operator-5dc4b5fb54-k5nrl   1/1     Running   0          4m15s
strimzi-cluster-operator-6c84667cb8-2n9f9    1/1     Running   0          5m59s
```

## Testing the Pipeline

### Expose Services

To test the data pipeline, expose the ingress and aggregator services:

```bash
# Expose ingress for archive uploads
oc create route edge ingress \
  --service=ingress \
  --port=3000 \
  -n edp-processing

# Expose aggregator for querying results
oc create route edge aggregator \
  --service=aggregator \
  --port=8082 \
  -n edp-processing

# Expose smart-proxy
oc create route edge smart-proxy \
  --service=smart-proxy \
  --port=8080 \
  -n edp-processing
```

### Option 1: Manual Upload Test Archive

For quick testing, you can manually upload a test archive:

```bash
# Setup Python environment (first time only)
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Install molodec for generating test archives
export PIP_INDEX_URL=https://repository.engineering.redhat.com/nexus/repository/insights-qe/simple
pip install -U molodec

# Upload test archive
INGRESS_URL=$(oc get route ingress -n edp-processing -o jsonpath='{.spec.host}')
python test_upload.py upload https://$INGRESS_URL
```

Expected output:
```
Uploading to: https://ingress-edp-processing.apps...
Status Code: 202
✅ Archive uploaded successfully!
```

### Option 2: Configure Local Insights Pipeline

This option configures the insights pipeline to use your local EDP stack instead of Red Hat's cloud services.

**Basic pipeline** (works on any OpenShift cluster):
1. **insights-operator** → uploads cluster data to local **ingress:3000**
2. Processing pipeline → processes the data and stores in database

**Full pipeline with insights-client** (requires ACM):
3. **insights-client** → fetches results from **identity-injector:8080** → **smart-proxy:8080** → **aggregator:8082** and creates PolicyReports

#### Step 1: Ingress and Identity-Injector Configuration

The deployment uses two key components for authentication:

**Identity-Injector** (Nginx proxy):
- Receives requests from insights-operator and insights-client
- Adds `x-rh-identity` header with test credentials (org_id: 000001, account: 0000001)
- Routes uploads to ingress and queries to smart-proxy
- Acts like 3scale in production environments

**Ingress** (`quay.io/cloudservices/insights-ingress:latest`):
- **Authentication enabled** (`INGRESS_AUTH=true`) - required to parse x-rh-identity headers
- **Kafka broker connection** configured via `INGRESS_KAFKABROKERS` environment variable
- Stores archives in MinIO and publishes messages to Kafka

No manual configuration is needed - the deployment YAML includes all necessary environment variables.

#### Step 2: Configure insights-operator to Upload Locally

By default, insights-operator uploads to Red Hat's cloud (`console.redhat.com`). Configure it to use your local identity-injector instead:

```bash
# Create a support Secret to override the upload and query endpoints
# Note: Must be a Secret (not ConfigMap) and must include full path
cat <<EOF | oc apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: support
  namespace: openshift-config
type: Opaque
stringData:
  endpoint: "http://identity-injector.edp-processing.svc.cluster.local:8080/api/ingress/v1/upload"
  insights-url: "http://identity-injector.edp-processing.svc.cluster.local:8080/api/v2"
EOF

# Restart insights-operator to pick up the new configuration
oc delete pod -n openshift-insights -l app=insights-operator

# Wait for it to come back up
oc wait --for=condition=ready pod -l app=insights-operator -n openshift-insights --timeout=120s
```

**What these settings do:**
- `endpoint`: Where insights-operator **uploads** cluster archives (identity-injector → ingress)
- `insights-url`: Where insights-operator **queries** for reports (identity-injector → smart-proxy)

**Verify insights-operator configuration:**

```bash
# Check the configuration is loaded
oc logs -n openshift-insights deployment/insights-operator --tail=100 | grep -A 15 "Configuration is"

# Expected output should show:
# uploadEndpoint: http://identity-injector.edp-processing.svc.cluster.local:8080/api/ingress/v1/upload
```

**Understanding Collection Schedule:**

Insights-operator collects data on a **2-hour periodic schedule** by default.

**To trigger an immediate collection:**

```bash
# Restart the insights-operator pod to trigger immediate collection
oc delete pod -n openshift-insights -l app=insights-operator

# Wait for it to restart
oc wait --for=condition=ready pod -l app=insights-operator -n openshift-insights --timeout=60s

# Watch it gather and upload (happens within ~2 minutes of restart)
oc logs -n openshift-insights deployment/insights-operator -f | grep -E "Running clusterconfig|Uploaded"

# Expected output:
# Running clusterconfig gatherer
# Uploading application/vnd.redhat.openshift.periodic to http://identity-injector.edp-processing.svc.cluster.local:8080/api/ingress/v1/upload
# Uploaded report successfully in XXXms
```

**Check when the last collection occurred:**

```bash
oc get insightsoperator cluster -o jsonpath='{.status.gatherStatus.lastGatherTime}' && echo
```

**Verify archive is being processed:**

After insights-operator uploads an archive, verify it flows through the pipeline:

```bash
# Run the pipeline verification script
./verify-pipeline.sh
```

This will check:
- Insights-operator upload status
- Ingress received the payload
- Archive processing and storage
- ACM insights-client queries (if ACM is installed)

#### Step 3: Configure insights-client to Fetch from Local Stack (Optional - Requires ACM)

**Note:** This step is **optional** and requires ACM (Advanced Cluster Management) to be installed. The insights-client deployment runs in the `open-cluster-management` namespace which is created by ACM. Skip this step if you don't have ACM installed.

The insights-client fetches processed reports and creates PolicyReports in your cluster. We configure it to use the **identity-injector** service which acts like 3scale in production - it adds the x-rh-identity header before forwarding to smart-proxy:

```bash
# Pause the MultiClusterHub operator to prevent it from reverting our changes
oc annotate multiclusterhub multiclusterhub -n open-cluster-management mch-pause=true --overwrite

# Configure insights-client to use identity-injector
# Note: insights-client appends "/cluster/{id}/reports" to this URL
oc set env deployment/insights-client -n open-cluster-management \
  CCX_SERVER=http://identity-injector.edp-processing.svc.cluster.local:8080/api/v2

# Wait for the rollout to complete
oc rollout status deployment/insights-client -n open-cluster-management --timeout=120s
```

**Verify insights-client configuration:**

```bash
# Check the environment variable is set correctly
oc exec -n open-cluster-management deployment/insights-client -- env | grep CCX_SERVER
# Expected output: CCX_SERVER=http://identity-injector.edp-processing.svc.cluster.local:8080/api/v2

# Watch the insights-client logs
oc logs -n open-cluster-management deployment/insights-client -f

# Expected output:
# Creating Request for cluster local-cluster (...) using Insights URL http://identity-injector.edp-processing.svc.cluster.local:8080/api/v2/cluster/.../reports
# Cluster local-cluster (...) is healthy. Skipping PolicyReport creation...
```

### Watch the Complete Processing Flow

After configuring insights-operator (and optionally insights-client), watch the data flow through the pipeline:

```bash
# 1. Watch insights-operator upload archive to identity-injector
oc logs -n openshift-insights deployment/insights-operator -f | grep -i upload

# 2. Watch identity-injector proxy to ingress
oc logs -n edp-processing deployment/identity-injector --tail=20 -f

# 3. Watch ingress receive the upload and send to Kafka
oc logs -n edp-processing deployment/ingress --tail=20 -f

# 4. Watch ccx-data-pipeline consume from Kafka and process rules
oc logs -n edp-processing deployment/ccx-data-pipeline --tail=50 -f

# 5. Watch db-writer write results to PostgreSQL
oc logs -n edp-processing deployment/db-writer --tail=20 -f

# 6. Watch insights-client fetch results via identity-injector
oc logs -n open-cluster-management deployment/insights-client -f

# 7. Check PolicyReports created by insights-client
oc get policyreports -A

# 8. View detailed PolicyReport for local-cluster
oc describe policyreport -n local-cluster local-cluster-policyreport
```

### Verify the Pipeline is Working

**Quick verification using the script:**

```bash
# Run basic checks
./verify-pipeline.sh

# Run with verbose output (shows commands and matching log lines)
./verify-pipeline.sh -v

# View detailed PolicyReport (if ACM is installed)
oc describe policyreport -n local-cluster local-cluster-policyreport
```

**Manual verification:**

Even if your cluster has 0 recommendations (healthy cluster), you can verify the pipeline is processing data:

```bash
# 1. Check insights-operator is uploading (look for recent timestamp)
oc logs -n openshift-insights deployment/insights-operator --since=3h | grep "Uploaded report successfully"

# 2. Check ingress received uploads
oc logs -n edp-processing deployment/ingress --since=3h | grep "Payload received" | tail -5

# 3. Check Kafka has messages
oc exec -n kafka edp-kafka-dual-role-0 -- bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 \
  --describe --group ccx_data_pipeline

# Expected: CURRENT-OFFSET and LOG-END-OFFSET should match (LAG = 0)

# 4. Check db-writer processed results (even if 0 recommendations)
oc logs -n edp-processing deployment/db-writer --since=3h | grep "processing message"

# You should see messages like:
# "message":"started processing message"
# "cluster":"00ff20e8-2326-4373-bce0-194ec01a59d1"
# "issues found":0  <- This is normal for a healthy cluster!
# "message":"Stored info report"
```

### Query Results from Aggregator

```bash
# Get the aggregator URL
AGGREGATOR_URL=$(oc get route aggregator -n edp-processing -o jsonpath='{.spec.host}')

# Query cluster reports (replace with your test cluster ID)
CLUSTER_ID="9f1511c6-6ef4-48ef-8fe9-e6dfea7076f0"
curl -sk "https://$AGGREGATOR_URL/api/v1/organizations/1/clusters/$CLUSTER_ID/reports" | jq
```

### Query Results from Smart Proxy

```bash
# Get the smart-proxy URL
SMART_PROXY_URL=$(oc get route smart-proxy -n edp-processing -o jsonpath='{.spec.host}')

# Identity header (base64 encoded: {"identity": {"type": "User", "account_number": "0000001", "org_id": "000001", "internal": {"org_id": "000001"}}})
IDENTITY_HEADER="eyJpZGVudGl0eSI6IHsidHlwZSI6ICJVc2VyIiwgImFjY291bnRfbnVtYmVyIjogIjAwMDAwMDEiLCAib3JnX2lkIjogIjAwMDAwMSIsICJpbnRlcm5hbCI6IHsib3JnX2lkIjogIjAwMDAwMSJ9fX0="

# Query cluster reports with enriched content
CLUSTER_ID="9f1511c6-6ef4-48ef-8fe9-e6dfea7076f0"
curl -sk -H "x-rh-identity: $IDENTITY_HEADER" \
  "https://$SMART_PROXY_URL/api/v1/clusters/$CLUSTER_ID/report" | \
  jq '.report.data[] | {rule_id, description, total_risk, resolution}'
```

## Configuration

### Image Registry Credentials

Before deploying, ensure you have valid credentials for the image registry (quay.io). These are required to pull the EDP service images. You'll need:
- Quay.io username
- Quay.io password or robot token

The credentials are configured during Step 3 of the deployment process.

### Default Credentials (Development Only)

**PostgreSQL:**
- Username: `user`
- Password: `password`
- Aggregator DB: `aggregator`
- Notification DB: `notifications`

**Redis:**
- Password: `password`

**Kafka:**
- No authentication (PLAINTEXT)

**MinIO:**
- Access Key: `minio`
- Secret Key: `minio123`

**IMPORTANT:** For production, update secrets in `deploy/01-secrets.yaml` before deploying.

### Service Endpoints

**Infrastructure:**
- `postgresql.edp-processing.svc.cluster.local:5432` - Aggregator PostgreSQL
- `notification-db.edp-processing.svc.cluster.local:5432` - Notification PostgreSQL
- `edp-kafka-kafka-bootstrap.kafka.svc.cluster.local:9092` - Kafka broker
- `redis.edp-processing.svc.cluster.local:6379` - Redis cache
- `minio.edp-processing.svc.cluster.local:9000` - MinIO S3 API
- `minio.edp-processing.svc.cluster.local:9001` - MinIO Console

**Application Services:**
- `ingress:3000` - Archive upload endpoint
- `aggregator:8082` - Aggregator REST API
- `smart-proxy:8080` - Smart Proxy unified API
- `content-service:8081` - Content Service API


## Cleanup

To remove the entire EDP stack:

```bash
# Delete application services
oc delete -f deploy/08-identity-injector.yaml
oc delete -f deploy/07-upgrades.yaml
oc delete -f deploy/06-api-services.yaml
oc delete -f deploy/05-writers.yaml
oc delete -f deploy/04-ingestion.yaml

# Delete infrastructure
oc delete -f deploy/02-infrastructure.yaml

# Delete secrets
oc delete -f deploy/01-secrets.yaml

# Delete Kafka
oc delete -f deploy/03-kafka-strimzi.yaml

# Delete namespace
oc delete -f deploy/00-namespace.yaml

# Delete Kafka namespace
oc delete namespace kafka
```


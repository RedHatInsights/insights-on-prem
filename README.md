# Insights on Premise

Insights on Premise aims to provide recommendations based on Insights archives in environments that **cannot reach console.redhat.com**. Specifically it is designed to be deployed in ACM clusters and for providing rule-based recommendations for managed clusters.

![Insights on Prem - High-level overview of the architecture](docs/insights-on-prem-overview.png)

## Contents

- [Insights on Premise](#insights-on-premise)
  - [Contents](#contents)
  - [Deployment to ACM Cluster](#deployment-to-acm-cluster)
    - [Prerequisites](#prerequisites)
    - [Deployment steps](#deployment-steps)
      - [Secrets](#secrets)
      - [Configuration](#configuration)
    - [Verify Deployment](#verify-deployment)
  - [Viewing Results in the ACM Console](#viewing-results-in-the-acm-console)
  - [On-Demand Data Gathering](#on-demand-data-gathering)
    - [How to Trigger](#how-to-trigger)
    - [Monitoring](#monitoring)
    - [Cleanup](#cleanup)
  - [Triggering Sample Results](#triggering-sample-results)
    - [Upgrade risk predictions](#upgrade-risk-predictions)
    - [Cluster recommendations](#cluster-recommendations)
  - [Database Access](#database-access)
  - [API Endpoints](#api-endpoints)
    - [Upload Archive](#upload-archive)
    - [Get Cluster Report](#get-cluster-report)
    - [Batch Upgrade Risk Predictions](#batch-upgrade-risk-predictions)
    - [Get Request Processing Status (on-demand data gathering)](#get-request-processing-status-on-demand-data-gathering)
    - [Get Request Report (on-demand data gathering)](#get-request-report-on-demand-data-gathering)
    - [Health Check](#health-check)
    - [API Documentation](#api-documentation)
  - [Running Locally with Docker Compose](#running-locally-with-docker-compose)
  - [Hermetic Builds](#hermetic-builds)
    - [Regenerating requirements.txt / requirements-build.txt / rpms.in.yaml](#regenerating-requirementstxt--requirements-buildtxt--rpmsinyaml)
  - [Building and Pushing Multiarch Image](#building-and-pushing-multiarch-image)
  - [License](#license)

## Deployment to ACM Cluster

### Prerequisites

Before going forward with deployment steps, check that:

- The hub is running on ACM 2.17.1+ and all clusters in the fleet are running OpenShift version >= 4.20.
- MultiClusterHub is created in `open-cluster-management` namespace (it can take several minutes before all components are started).
- Hub cluster self-management is enabled (default ACM behavior). The hub must be imported into ACM as a managed cluster (with the `local-cluster: "true"` label) so that Policies can target it for certificate management.
- Pull secret for [quay.io/ccxdev/insights-on-premise-poc](https://quay.io/repository/ccxdev/insights-on-premise-poc) repository is saved as `deploy/02-pull-secret.yml` in the following format:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: ccxdev-insights-on-prem-pull-secret
  namespace: insights-on-prem
data:
  .dockerconfigjson: <INSERT YOUR BASE64-ENCODED PULL SECRET HERE>
type: kubernetes.io/dockerconfigjson
```

- (optional) Multicluster Observability Operator is deployed according to [these instructions](https://github.com/stolostron/multicluster-observability-operator/tree/main?tab=readme-ov-file#run-the-operator-in-the-cluster). **This step is required for enabling update risk predictions.**

### Deployment steps

After confirming that prerequisites are met, you can install the addon with:

```bash
oc apply -f deploy/
```

This applies all manifests in `deploy/` to the cluster. It can take a while until all resources are properly deployed.

> **Note:** Installation of Insights on Prem redirects Insights endpoints in ACM console and client deployments from console.redhat.com to Insights on Prem deployed on the hub cluster.

#### Secrets

The postgres password is stored in the secret `insights-postgres` in the `insights-on-prem` namespace, defined in `deploy/03-postgres.yml`. Note that this is not the best practice, so please use the preferred method on your cluster to define the secret. We kept it there to make it easier to deploy the application without human intervention.

#### Configuration

The application is configured via environment variables set on the `insights-on-prem` deployment in the `insights-on-prem` namespace. The following variables can be tuned after deployment:

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `DB_RETENTION_HOURS` | `24` | How long to keep processed records in the database before automatic cleanup |
| `DB_CLEANUP_INTERVAL_MINUTES` | `60` | How often the background cleanup task runs (in minutes) |
| `MAX_FILE_SIZE` | `104857600` (100 MB) | Maximum uploaded archive file size in bytes |
| `THANOS_URL` | `https://rbac-query-proxy.open-cluster-management-observability.svc.cluster.local:8443` | Thanos query endpoint URL (only relevant if MCO is deployed) |
| `THANOS_QUERY_TIMEOUT_SECONDS` | `10` | Timeout for Thanos queries |
| `THANOS_QUERY_LOOKBACK_MINUTES` | `60` | How far back to look when querying Thanos metrics |
| `MTLS_ENABLED` | `true` (in-cluster) | Whether the server uses mTLS on port 8443 instead of plain HTTP on 8000 |

Database connection is configured through the `insights-postgres` secret (see [Secrets](#secrets) above). The variables `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB` are all read from that secret in the default deployment manifests.

To change a setting, patch the deployment:

```bash
oc set env deployment/insights-on-prem -n insights-on-prem DB_RETENTION_HOURS=48
```

### Verify Deployment

After manifests are applied, you can check that everything was properly deployed by running the following:

```bash
# Check pod status
oc get pods -n insights-on-prem

# Check policy compliance (all should be Compliant)
oc get policy -n insights-on-prem

# Verify insights-client and console env overrides via MCH
oc get mch multiclusterhub -n open-cluster-management -o json | jq '.spec.overrides.components'

# Verify console URP URL
oc get configmap console-config -n open-cluster-management -o jsonpath='{.data.UPGRADE_RISKS_PREDICTION_URL}'

# Check logs
oc logs -f deployment/insights-on-prem -n insights-on-prem
```

## Viewing Results in the ACM Console

Insights recommendations are visible in the cluster console under `Fleet Management -> Home -> Overview`, or go directly to this URL:

```text
https://<CLUSTER_CONSOLE_URL>/multicloud/home/overview
```

![ACM Fleet Overview - Insights section showing all panels populated by Insights on Premise](docs/fleet-overview-ui.png)

The Insights section of that page has four panels:

| Panel | Depends on Insights on Prem | Depends on MCO |
| ------- | :---------------------------: | :---------------: |
| Cluster recommendations | Yes | No |
| Update risk predictions | Yes | Yes |
| Alerts | No | Yes |
| Failing operators | No | Yes |

**Cluster recommendations** are based on `PolicyReport` custom resources created by `insights-client` in each managed cluster's namespace. **Update risk predictions** are served by Insights on Prem, but rely on metrics collected by MCO into Thanos. **Alerts** and **Failing operators** are read directly from Thanos by the ACM console and do not involve Insights on Prem at all.

## On-Demand Data Gathering

On-demand data gathering allows triggering Insights data collection outside the regular periodic schedule. Instead of waiting for the next periodic upload (default 2h, set to 1m by `deploy/14-hub-config.yml`), you can request an immediate gather-and-upload cycle and get results for that specific request.

> **Note:** Conditional data gathering is not supported at this moment. Disable the `conditional` gatherer in the `DataGather` CR to avoid unnecessary calls to `console.redhat.com` for gathering rules (as shown in the following section).

### How to Trigger

Create a `DataGather` custom resource:

```bash
oc apply -f - <<'EOF'
apiVersion: insights.openshift.io/v1
kind: DataGather
metadata:
  name: on-demand-test
spec:
  gatherers:
    mode: Custom
    custom:
      configs:
      - name: conditional
        state: Disabled
  storage:
    type: Ephemeral
EOF
```

The insights-operator detects the new CR, creates a Job in `openshift-insights`, and the Job:

1. Runs all gatherers and writes an archive
2. Uploads the archive to the on-prem service
3. Polls the processing status endpoint until the archive is processed
4. Logs success — the operator then fetches the report for the specific request ID

### Monitoring

Watch the Job and its logs:

```bash
# Check job status
oc get jobs -n openshift-insights | grep -v periodic

# Follow the job pod logs
oc logs -n openshift-insights -l job-name=on-demand-test -f

# Check the DataGather CR status
oc get datagather on-demand-test -o yaml
```

The `DataGather` CR status conditions show the lifecycle:

- `DataRecorded` — archive written to disk
- `DataUploaded` — archive uploaded to the on-prem service
- `DataProcessed` — archive processed and results available

### Cleanup

Jobs and `DataGather` CRs older than 24 hours are automatically pruned by the Insights Operator. To delete manually:

```bash
oc delete datagather on-demand-test
oc delete job on-demand-test -n openshift-insights
```

## Triggering Sample Results

After deploying Insights on Prem on a healthy cluster, the Fleet Overview panels will likely be empty:

![ACM Fleet Overview - Insights section not showing any results](docs/fleet-overview-empty.png)

In case you want to quickly trigger some results, you can run `test_ui.sh` with the hub cluster kubeconfig. The script will execute changes on cluster (uploading metrics, creatings resources) in order to trigger both upgrade risk predictions and cluster recommendations for the hub cluster:

```bash
./test_ui.sh
```

If you want to revert the changes, see the script comments for cleanup instructions.

You can also trigger each section manually as described below.

### Upgrade risk predictions

The upgrade prediction service flags a cluster as at-risk when it detects two or more critical alerts. Create a `PrometheusRule` that fires two always-on critical alerts:

```bash
oc apply -f tests/ui/critical-alerts.yaml
```

The alerts need to reach Thanos before results appear (typically 2-5 minutes). By default the on-prem service queries Thanos at `now - 60 minutes`, so freshly fired alerts won't be visible. To query at the current timestamp instead:

```bash
oc set env deployment/insights-on-prem -n insights-on-prem THANOS_QUERY_LOOKBACK_MINUTES=0
```

After the alerts propagate, you should see one cluster not recommended for update in the ACM console.

To clean up the changes, run:

```bash
oc delete prometheusrule insights-test-alerts -n openshift-monitoring
oc set env deployment/insights-on-prem -n insights-on-prem THANOS_QUERY_LOOKBACK_MINUTES-
```

### Cluster recommendations

To trigger a sample Insights recommendation, at least one insights-core rule condition has to be met. The easiest way is to create a `ValidatingWebhookConfiguration` with a timeout larger than the default, which triggers the [webhook_timeout_is_larger_than_default](https://gitlab.cee.redhat.com/ccx/ccx-rules-ocp/-/blob/master/ccx_rules_ocp/external/rules/webhook_timeout_is_larger_than_default.py) rule:

```bash
oc apply -f tests/ui/webhook-trigger.yaml
```

Depending on the frequency of archive uploads from Insights Operator (set to 1 minute by `deploy/14-hub-config.yml`, but default value is 2 hours), the recommendation and the `PolicyReport` should be created. You can verify either via ACM console, or with:

```bash
oc get policyreport --all-namespaces
```

After that you should be able to see at least one policyreport for the `local-cluster` (that is, for the ACM hub):

```
NAMESPACE       NAME                         PASS   FAIL   WARN   ERROR   SKIP   AGE
local-cluster   local-cluster-policyreport   0      1      0      0       0      4m
```

To clean up the changes, run:

```bash
oc delete validatingwebhookconfiguration insights-test-webhook
```

## Database Access

The application deploys its own PostgreSQL database. Data older than 24 hours is cleaned up automatically by default (configurable via `DB_RETENTION_HOURS` environment variable).

**Connect to database:**

```bash
# Locally
docker compose exec postgres psql -U insights -d insights

# In cluster
oc exec -it deployment/insights-postgres -n insights-on-prem -- psql -U insights -d insights
```

## API Endpoints

### Upload Archive

```text
POST /api/ingress/v1/upload
```

Upload an Insights archive for processing.

**Example:**

```bash
curl -X POST http://localhost:8000/api/ingress/v1/upload -F "file=@/path/to/archive.tar.gz"
```

### Get Cluster Report

```text
GET /api/v2/cluster/{cluster_id}/reports
```

Retrieve processed report for a cluster.

### Batch Upgrade Risk Predictions

```text
POST /api/insights-results-aggregator/v2/upgrade-risks-prediction
```

Returns upgrade risk predictions for a list of clusters. These predictions are based on alerts and operator conditions that are retrieved from Thanos instance.

### Get Request Processing Status (on-demand data gathering)

```text
GET /api/v2/cluster/{cluster_id}/request/{request_id}/status
```

Check whether an on-demand data gathering request has been processed. Returns 404 while processing is in progress (the operator retries), 200 once ready.

### Get Request Report (on-demand data gathering)

```text
GET /api/v2/cluster/{cluster_id}/request/{request_id}/report
```

Retrieve the simplified report for a specific on-demand data gathering request ID.

### Health Check

```text
GET /health
```

### API Documentation

When running Insights on Prem locally, you can access documentation via these endpoints:

- Swagger UI: <http://localhost:8000/docs>
- ReDoc: <http://localhost:8000/redoc>

## Running Locally with Docker Compose

For purposes of running the addon locally without need for the cluster, we maintain `docker-compose.yml`, so `docker-compose` is required. Alternatively, you can also use `podman-compose`. The commands are the same.

1. **Start services:**

   ```bash
   docker compose up -d
   ```

2. **Run database migrations:**

   ```bash
   docker compose exec app alembic -c migrations/alembic.ini upgrade head
   ```

3. **Verify:**

   ```bash
   curl http://localhost:8000/health
   ```

4. **View logs:**

   ```bash
   docker compose logs -f app
   ```

5. **Stop services:**

   ```bash
   docker compose down
   ```

## Hermetic Builds

The Dockerfile is built hermetically by Konflux (network access disabled during the build), using [Hermeto](https://hermetoproject.github.io/hermeto/) to prefetch pip and RPM dependencies beforehand. See `requirements-in.txt` (source for `pip-compile`), `requirements.txt`/`requirements-build.txt` (pinned lockfiles), and `rpms.in.yaml`/`rpms.lock.yaml` (RPM lockfile, regenerated via `scripts/update_rpm_lockfile.sh`).

### Regenerating requirements.txt / requirements-build.txt / rpms.in.yaml

To add or upgrade a Python dependency, edit `requirements-in.txt`, then regenerate the pinned files. **Always do this inside a `linux/amd64` Python 3.12 container** (matching the base image and Konflux's build platform) — running `pip-compile` on macOS/arm64 silently drops dependencies whose markers only match `x86_64`/`aarch64` (e.g. SQLAlchemy's `greenlet`), since pip-compile resolves environment markers against the machine it runs on, not the target platform:

```bash
# Prefer the helper script (also used by ccx-rules-releaser):
podman run --rm --platform linux/amd64 -v "$(pwd):/work:Z" -w /work python:3.12-slim \
  bash scripts/update_requirements.sh

# Equivalent manual commands:
podman run --rm --platform linux/amd64 -v "$(pwd):/work:Z" -w /work python:3.12-slim bash -c '
  set -euo pipefail
  pip install -q pip-tools pybuild-deps
  pip-compile --output-file=requirements.txt requirements-in.txt
  pybuild-deps compile --generate-hashes --output-file=requirements-build.txt requirements.txt
'
```

`requirements.txt` is the fully-pinned runtime lockfile; `requirements-build.txt` lists the build-backend sdists (e.g. `setuptools`, `cython`, `maturin`) Hermeto needs to prefetch so packages without prebuilt wheels can be built from source in the hermetic build.

Some RPMs (e.g. `postgresql-devel`) are only available on the entitled RHEL CDN, not the public UBI repos. Konflux's `prefetch-dependencies` task needs an `activation-key` secret in the `obsint-processing-tenant` namespace to authenticate to that CDN — without it, prefetching fails with a misleading `SSLCertVerificationError: self-signed certificate in certificate chain`. This secret is namespace-scoped (shared with `rules-containers`), see the value in Bitwarden, so it only needs to be created once per tenant:

```bash
oc create -f <path-to>/activation-key-secret.yaml
```

You may need to follow <https://konflux.pages.redhat.com/docs/users/building/activation-keys-subscription.html#Create-custom-activation-key-secret> to troubleshoot any issues.

`rpms.in.yaml`'s `context.image` must be kept in sync with the Dockerfile's `FROM` tag. `rpm-lockfile-prototype` resolves dependencies against the RPMs already installed in that image, so pointing it at a different image (or a stale tag) can lock in package versions (e.g. `glibc-devel`) that don't match what's actually baked into the real base image, causing `microdnf install` to fail in the hermetic build with `nothing provides glibc = <locked-version>`. Whenever you bump the base image tag in the Dockerfile, update `context.image` to match and rerun `scripts/update_rpm_lockfile.sh`.

## Building and Pushing Multiarch Image

In case you need to manually build and push a multiarch (amd64, arm64) image to Quay, run these commands (this step is necessary because cluster nodes may run on different architecture than the development environment):

```bash
# Login to Quay
docker login quay.io

# Build and push multiarch image
docker buildx build --platform linux/amd64,linux/arm64 \
  -t quay.io/NAMESPACE/IMAGE:TAG \
  --push .
```

## License

This project is licensed under the AGPL v3 - see the [LICENSE](LICENSE) file for details.

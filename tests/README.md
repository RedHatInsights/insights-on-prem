# Test Infrastructure

This directory contains the test runtime image and test suites for insights-on-prem.

## Directory Structure

```
tests/
├── Dockerfile         # Test runtime image (all deps, no test code)
├── requirements.txt   # Python test dependencies
├── integration/       # Integration tests
└── e2e/               # End-to-end tests
```

## Test Runtime Image

The `Dockerfile` builds a runtime image with all test tooling pre-installed (Python, pytest, curl, tar, jq). Test code is **not** included in the image -- it is cloned at runtime from the PR or branch being tested.

The `oc` CLI is intentionally **not** baked into the image. It is downloaded at runtime from the target OCP cluster's downloads route to guarantee version alignment:

```bash
curl -kL https://downloads-openshift-console.apps.<cluster>/amd64/linux/oc.tar.gz | tar xz -C /usr/local/bin
```

### CI Pipelines

The test image has two Tekton pipelines in `.tekton/`:

- **`insights-on-prem-tests-push.yaml`** -- Builds and pushes the image on merge to `master`. Only triggers when `tests/Dockerfile` or `tests/requirements.txt` change.
- **`insights-on-prem-tests-pull-request.yaml`** -- Build validation only. Verifies the Dockerfile builds successfully on PRs but the resulting image is not used for testing. Security scans are skipped. Only triggers when `tests/Dockerfile` or `tests/requirements.txt` change.

### Image Location

The image is private and hosted at:

```
quay.io/ccxdev/insights-on-prem-tests:latest
```

The repository is managed directly in the `ccxdev` Quay org (not via Konflux ImageRepository/Component CRs, to avoid SNAPSHOT coordination issues). Push credentials are stored as a `dockerconfigjson` Secret in the `obsint-processing-tenant` namespace, linked to the pipeline service account.

For Prow periodic jobs, the image will need to be made accessible separately. See `TODO.md` for options.

## Integration Tests

The `integration/` directory contains tests that run against a live OpenShift cluster with ACM installed. These tests are designed to be portable across multiple execution contexts:

| Context | How it runs |
|---------|-------------|
| **Konflux integration pipeline** | Automatic on every PR via IntegrationTestScenario |
| **Job inside the cluster** | Deploy as a Job with in-cluster ServiceAccount |
| **Developer machine** | `KUBECONFIG=~/.kube/config NAMESPACE=insights-on-prem pytest tests/integration/` |

Tests discover the cluster via the standard `KUBECONFIG` env var (or in-cluster config when running as a Job). Service-specific configuration is passed via env vars (`NAMESPACE`, `DEPLOYMENT_NAME`).

### Running Locally

```bash
export KUBECONFIG=~/.kube/config
export NAMESPACE=insights-on-prem
pytest tests/integration/ -v
```

### CI Pipeline

The smoke test pipeline (`ci/test-pipelines/insights-on-prem-smoke-test-pipeline.yaml`) runs on every PR:

1. Claims a cluster from the `obs` pool (~5-10 min)
2. Installs ACM with Basic availability (~15-25 min)
3. Deploys the PR-built service image
4. Runs `pytest tests/integration/test_smoke.py`
5. Deprovisions the cluster

The IntegrationTestScenario is currently set to **optional** (advisory, not blocking).

## Adding Dependencies

- **Python packages:** Add to `requirements.txt`, the image will be rebuilt on next merge.
- **System packages:** Add to the `microdnf install` line in `Dockerfile`.
- **New dependency in a PR:** If a PR introduces a test that needs a new dep not yet in the image, install it at runtime as an escape hatch (e.g. `pip install <pkg>` in the test entrypoint). Then update `requirements.txt` or `Dockerfile` in the same PR so the image is rebuilt on merge.

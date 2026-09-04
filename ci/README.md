# IOP E2E Pipeline

End-to-end test pipeline for Insights on Premise using Konflux EaaS
ephemeral HyperShift clusters.

## Pipeline overview

The pipeline provisions two ephemeral OCP clusters on AWS (a hub and a
managed cluster), installs ACM, deploys IOP from the Konflux Snapshot
image, imports the managed cluster into the ACM hub, and runs smoke
tests.

```
eaas-provision-space
    |
    +---> provision-hub (m5.2xlarge)     provision-managed (m5.xlarge)
              |                                |
              v                                |
         acm-install                           |
              |                                |
              +---> iop-deploy                 |
              |         |                      |
              +---> import-managed-cluster <---+
                        |         |
                        v         v
                   iop-e2e-tests
```

Clusters are destroyed automatically when the PipelineRun is cleaned up
by Konflux. EaaS clusters have a hard 2-hour lifespan regardless.

## How triggering works

The pipeline is triggered through the Konflux **IntegrationTestScenario**
(ITS) named `insights-on-prem-eaas-e2e`, which is managed in the
`konflux-release-data` repository. The flow is:

1. A PR is merged (or a build completes) and Konflux creates a **Snapshot**
2. The `ci/trigger-e2e.sh` script labels the Snapshot with
   `test.appstudio.openshift.io/run=insights-on-prem-eaas-e2e`
3. The Konflux integration service picks up the label, creates a
   **PipelineRun** from the ITS definition, and passes the Snapshot JSON
   as a pipeline parameter
4. The PipelineRun appears in the Konflux UI and results are reported
   back to GitHub

## Why PipelineRun labels instead of pipeline parameters

Tekton pipeline parameters are **immutable after PipelineRun creation**.
Since the PipelineRun is created by the Konflux integration service (not
by us directly), there is no way to pass custom pipeline parameters
through the Snapshot label triggering flow.

We considered creating the PipelineRun directly (bypassing the ITS), but
that loses two important features:

- The PipelineRun does not appear in the Konflux UI under the ITS
- Test results are not reported back to GitHub as status checks

Instead, the pipeline reads **debug labels from its own PipelineRun
metadata** at runtime. The `konflux-integration-runner` service account
has RBAC to read PipelineRun objects in the tenant namespace, so tasks
can query their own labels with `oc get pipelinerun`.

The trigger script labels the Snapshot first (triggering the ITS), waits
for the PipelineRun to be created, and then labels the PipelineRun with
the requested debug flags. Since provisioning takes 10+ minutes, there is
plenty of time for the labels to be applied before any task reads them.

## Usage

### Basic trigger (no debug)

```bash
# Trigger for current HEAD
ci/trigger-e2e.sh

# Trigger for a specific commit
ci/trigger-e2e.sh abc1234

# Retrigger (force)
ci/trigger-e2e.sh --force
```

### Debug: hold clusters on failure

```bash
ci/trigger-e2e.sh --debug
```

When `--debug` is set, the script labels the PipelineRun with
`debug.iop/hold-on-failure=true`. If any task fails, the `finally` task:

1. Prints cluster credentials (API URL, console URL, username, password,
   `oc login` command, base64-encoded kubeconfig) to the task logs
2. Sleeps indefinitely to keep the PipelineRun alive (and therefore the
   EaaS clusters alive)

When done debugging, cancel the PipelineRun:

```bash
oc patch pipelinerun <name> -n obsint-processing-tenant \
  --type merge -p '{"spec":{"status":"CancelledRunFinally"}}'
```

You can also add the label manually to any running PipelineRun at any
time (before the failure occurs):

```bash
oc label pipelinerun <name> -n obsint-processing-tenant \
  debug.iop/hold-on-failure=true
```

### Test filter (placeholder)

```bash
ci/trigger-e2e.sh --test-filter smoke
```

Labels the PipelineRun with `debug.iop/test-filter=smoke`. The test task
reads this label at runtime. The actual test filtering logic is not yet
implemented (TODO).

### Get cluster credentials

After triggering with `--debug` and the pipeline has failed, extract
credentials and set up `oc` contexts:

```bash
# Auto-find the latest debug PipelineRun
ci/get-eaas-credentials.sh

# Or specify a PipelineRun name
ci/get-eaas-credentials.sh insights-on-prem-eaas-e2e-fxrpk
```

This creates two contexts you can switch between:

```bash
oc config use-context eaas-hub
oc config use-context eaas-managed
```

Your original `oc` context is preserved.

### Cleaning up after debugging

The `hold-clusters-on-failure` task sleeps indefinitely to keep the
clusters alive. **Cancel the PipelineRun when you are done** to free
resources:

```bash
oc patch pipelinerun <name> -n obsint-processing-tenant \
  --type merge -p '{"spec":{"status":"CancelledRunFinally"}}'
```

The `get-eaas-credentials.sh` script prints this command with the
correct PipelineRun name. EaaS clusters are also destroyed automatically
2 hours after creation, but cancelling early avoids wasting the pipeline
slot and compute.

### Combined

```bash
ci/trigger-e2e.sh --debug --test-filter smoke abc1234
```

## ITS timeout annotations

For the `hold-clusters-on-failure` finally task to have time to sleep,
the ITS in `konflux-release-data` needs timeout annotations:

```yaml
metadata:
  annotations:
    test.appstudio.openshift.io/pipeline_timeout: "4h"
    test.appstudio.openshift.io/tasks_timeout: "2h"
    test.appstudio.openshift.io/finally_timeout: "2h"
```

Without these, the default 1-hour PipelineRun timeout will kill the
finally task early.

## Prerequisites

- `oc` CLI, logged into the Konflux cluster
- `jq`
- Access to the `obsint-processing-tenant` namespace

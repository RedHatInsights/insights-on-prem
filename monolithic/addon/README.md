# Insights On-Premise ACM Addon

This directory contains the ACM addon manifests that replace `deploy.sh` with a proper,
continuously-reconciled deployment.

## How it works

All configuration is handled by a single `AddOnTemplate` (`04-addon-template.yaml`) which
the addon framework deploys via ManifestWork. A configure Job runs at install time to handle
dynamic operations that can't be expressed as static manifests:
- Copies `search-postgres` secret into the addon namespace
- Creates the HTTPS route
- Sets `CCX_SERVER` on `insights-client` and pauses MCH to prevent reversion
- Configures the console with the custom image and `UPGRADE_RISKS_PREDICTION_URL`
- Restarts `insights-operator` to pick up the new `insights-config` ConfigMap

## Files

| File | What it does |
|------|--------------|
| `01-namespace.yaml` | Namespace for addon resources + `ManagedClusterSetBinding` |
| `02-addon.yaml` | `ClusterManagementAddOn` — registers the addon in ACM |
| `03-placement.yaml` | Targets `local-cluster` (hub) only |
| `04-addon-template.yaml` | Everything: on-prem pod, service, RBAC, `insights-config`, configure Job |
| *(auto-created by addon-manager)* | `ManagedClusterAddOn` for `local-cluster` — created automatically based on the placement |

## Prerequisites

- OpenShift cluster with ACM installed and MCH in `Running` state
- MCO deployed — required for URP (provides Thanos for metrics). Follow the [MCO setup instructions](https://github.com/stolostron/multicluster-observability-operator/tree/main?tab=readme-ov-file#run-the-operator-in-the-cluster)
- CCXDEV-16237 merged into `stolostron/console` (for URP URL to work without the custom image — until then the addon deploys a custom console image automatically)

## Install

```bash
# 1. Create the pull secret for the on-prem service image.
#    insights-on-prem-poc namespace is created here manually because the pull secret
#    must exist before the addon's ManifestWork tries to pull the image.
#    (The addon also creates this namespace via ManifestWork, but the image pull happens first.)
oc create namespace insights-on-prem-poc
oc apply -f monolithic/deploy/ccxdev-insights-on-prem-poc-secret.yml -n insights-on-prem-poc

# 2. Apply the addon — the rest is automatic
oc apply -f monolithic/addon/
```

The addon-manager automatically creates the `ManagedClusterAddOn` for `local-cluster` based on the placement. A configure Job then runs to set up `CCX_SERVER`, create the HTTPS route, configure the console, and restart `insights-operator`. Watch progress with:

```bash
oc logs job/insights-on-prem-configure -n insights-on-prem-poc -f
```

After the Job completes, run `test_ui.sh` to set up test data and verify the UI.

## Uninstall

```bash
oc delete -f monolithic/addon/
oc delete namespace insights-on-prem-poc
```

## Known limitations

**PostgreSQL**: The addon temporarily copies the `search-postgres` secret from ACM's search
component. This is a shared database and not a long-term solution — the addon should
eventually provision its own PostgreSQL instance.

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
| `09-managed-cluster-addon.yaml` | Enables the addon on `local-cluster` |

## Prerequisites

- ACM installed with MCH Running
- MCO deployed (required for URP — provides Thanos)
- Pull secret created: `oc apply -f deploy/ccxdev-insights-on-prem-poc-secret.yml -n insights-on-prem-poc`
- CCXDEV-16237 merged into `stolostron/console` (for URP URL to work without the custom image)

## Known limitations

**PostgreSQL**: The addon temporarily copies the `search-postgres` secret from ACM's search
component. This is a shared database and not a long-term solution — the addon should
eventually provision its own PostgreSQL instance.

## Install

```bash
oc apply -f addon/
```

## Uninstall

```bash
oc delete -f addon/
```

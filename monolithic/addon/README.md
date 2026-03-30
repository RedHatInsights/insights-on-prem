# Insights On-Premise ACM Addon

This directory contains the ACM addon manifests that replace `deploy.sh` with a proper,
continuously-reconciled deployment.

## Why an addon vs deploy.sh

`deploy.sh` pauses the MCH operator (`mch-pause=true`) to prevent it from reverting
`CCX_SERVER` and the console image. This is fragile — if MCH is unpaused for any reason,
all changes are lost and `deploy.sh` must be re-run.

The addon uses `ConfigurationPolicy` with `remediationAction: enforce` instead. ACM
continuously reconciles the policy, so MCH cannot permanently revert the configuration.
No pause needed.

## Files

| File | What it does |
|------|--------------|
| `01-namespace.yaml` | Namespace for addon resources |
| `02-addon.yaml` | `ClusterManagementAddOn` — registers the addon in ACM |
| `03-placement.yaml` | Targets `local-cluster` (hub) only |
| `04-addon-template.yaml` | Deploys the on-prem pod, service, RBAC on the hub |
| `05-policy-insights-operator.yaml` | Enforces `insights-config` ConfigMap — redirects uploads to on-prem |
| `06-policy-insights-client.yaml` | Enforces `CCX_SERVER` on `insights-client` deployment |
| `07-policy-console.yaml` | Enforces `UPGRADE_RISKS_PREDICTION_URL` on console (requires CCXDEV-16237) |

## Prerequisites

- ACM installed with `open-cluster-management` and `governance-policy-framework` addons
- MCO deployed (required for URP — provides Thanos)
- `ccxdev-insights-on-prem-poc-pull-secret` in `insights-on-prem-poc` namespace
- CCXDEV-16237 merged into `stolostron/console` (for `07-policy-console.yaml` to take effect)

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

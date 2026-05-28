# Insights on Prem — Microservices (archived)

Deployment of the full console.redhat.com pipeline components individually on-premise. This approach was explored early on but we decided to pursue the monolithic architecture instead.

- **Deploy manifests** (`microservices/deploy/`) — Kubernetes YAMLs for namespace, secrets, Kafka (Strimzi), ingestion, writers, API services, upgrades, identity injector, and Thanos integration
- **Scripts** — `edp.sh` for setup, `verify-pipeline.sh` for validation, `test_upload.py` for testing uploads

---

The active monolithic approach lives on [`master`](https://github.com/RedHatInsights/insights-on-prem/tree/master).

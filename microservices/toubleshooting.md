## Troubleshooting

### Pods Not Starting

1. Check events:
   ```bash
   oc get events -n edp-processing --sort-by='.lastTimestamp'
   ```

2. Check PVC binding:
   ```bash
   oc get pvc -n edp-processing
   ```

3. Check resource availability:
   ```bash
   oc describe node <node-name>
   ```

### Database Connection Issues

1. Verify PostgreSQL is running:
   ```bash
   oc get pods -n edp-processing -l app=postgresql
   ```

2. Test connection from application pod:
   ```bash
   oc exec -it -n edp-processing deployment/aggregator -- /bin/sh
   # Inside pod
   nc -zv postgresql 5432
   ```

### Kafka Connection Issues

1. Verify Kafka is running:
   ```bash
   oc get kafka -n kafka
   oc get pods -n kafka -l strimzi.io/cluster=edp-kafka
   ```

2. Check Kafka logs:
   ```bash
   oc logs -n kafka edp-kafka-dual-role-0
   ```

3. Test cross-namespace connectivity from application pod:
   ```bash
   oc exec -it -n edp-processing deployment/ccx-data-pipeline -- /bin/sh
   # Inside pod
   nc -zv edp-kafka-kafka-bootstrap.kafka.svc.cluster.local 9092
   ```

### Service Not Receiving Data

1. Check if Kafka topics exist and have data
2. Verify consumer group is consuming:
   - Check logs of consumer services (ccx-data-pipeline, db-writer, etc.)
3. Verify network connectivity between services

## Upgrading

To upgrade service images:

1. Edit the image tags in addon templates:
   ```yaml
   image: quay.io/redhat-services-prod/.../service:NEW_TAG
   ```

2. Reapply:
   ```bash
   oc apply -f deploy/
   ```

ACM will handle rolling updates to managed clusters.

## Uninstall

### Remove from Specific Cluster

```bash
# Remove label from managed cluster
oc label managedcluster <cluster-name> edp-deployment-
```

### Complete Removal

```bash
# From hub cluster, delete all addon resources
oc delete -f deploy/

# On managed cluster, clean up namespaces if needed
oc delete namespace edp-processing
oc delete namespace kafka
```

**Note:** This will delete all data including PostgreSQL databases and Kafka topics. Persistent volume claims will be retained based on the `deleteClaim: false` setting.

## Support

For issues or questions:
- Check the troubleshooting section above
- Review logs from failing services
- Consult the ACM documentation: https://access.redhat.com/documentation/en-us/red_hat_advanced_cluster_management_for_kubernetes

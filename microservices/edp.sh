#!/bin/bash
# EDP Deployment Script
# Before running 'all' or 'databases', create Quay pull secret:
#   oc create ns edp-processing
#   oc create secret docker-registry quay-pull-secret --docker-server=quay.io \
#     --docker-username=<user> --docker-password=<pass> -n edp-processing
#   oc secrets link default quay-pull-secret --for=pull -n edp-processing

set -e
confirm() { read -p "$1 (y/n) " -n 1 -r && echo; [[ $REPLY =~ ^[Yy]$ ]]; }
wait_ready() { oc wait --for=condition=ready pod -l "$2" -n "$1" --timeout=300s && echo "✓ $3"; }
check_pod() { oc get pod -l "$2" -n "$1" -o jsonpath='{.items[0].status.phase}' 2>/dev/null | grep -q "Running" && echo "✓ $3" || { echo "❌ $3"; FAILED=1; }; }

setup_kafka() {
    echo "=== Kafka ==="
    if oc get ns kafka &>/dev/null; then
        confirm "Kafka namespace exists. Continue?" || return 1
    else
        oc create ns kafka
    fi
    oc create -f 'https://strimzi.io/install/latest?namespace=kafka' -n kafka 2>/dev/null || true
    wait_ready kafka name=strimzi-cluster-operator "Strimzi operator"
    oc apply -f deploy/03-kafka-strimzi.yaml
    oc wait kafka/edp-kafka --for=condition=Ready --timeout=300s -n kafka && echo "✓ Kafka cluster"
    oc get pods,kafkatopic -n kafka
}

setup_databases() {
    echo "=== Databases ==="
    oc get ns edp-processing &>/dev/null || oc apply -f deploy/00-namespace.yaml
    oc apply -f deploy/01-secrets.yaml
    oc apply -f deploy/02-infrastructure.yaml
    for svc in postgresql minio redis notification-db; do wait_ready edp-processing app=$svc $svc; done
    oc get pods -n edp-processing
}

setup_edp_services() {
    echo "=== Services ==="
    oc get ns edp-processing &>/dev/null || { echo "❌ Run databases first"; return 1; }
    oc get kafka edp-kafka -n kafka &>/dev/null || { echo "❌ Run kafka first"; return 1; }
    oc apply -f deploy/04-application-services.yaml
    for svc in ingress ccx-data-pipeline db-writer aggregator smart-proxy; do wait_ready edp-processing app=$svc $svc; done
    oc apply -f deploy/05-identity-injector.yaml
    wait_ready edp-processing app=identity-injector identity-injector
    oc get pods -n edp-processing
    echo "✓ All services deployed and ready"
}

expose_services() {
    echo "=== Routes ==="
    oc get ns edp-processing &>/dev/null || { echo "❌ Namespace not found"; return 1; }
    for svc in "ingress:3000" "aggregator:8082" "smart-proxy:8080" "content-service:8081"; do
        IFS=: read name port <<< "$svc"
        oc get route $name -n edp-processing &>/dev/null || oc create route edge $name --service=$name --port=$port -n edp-processing
    done
    for svc in ingress aggregator smart-proxy content-service; do
        printf "%-16s https://%s\n" "$svc:" "$(oc get route $svc -n edp-processing -o jsonpath='{.spec.host}')"
    done
}

configure_insights() {
    echo "=== Insights ==="
    oc get secret support -n openshift-config &>/dev/null && \
        oc get secret support -n openshift-config -o yaml > support-secret-backup.yaml && \
        oc delete secret support -n openshift-config
    oc create secret generic support \
        --from-literal=endpoint="http://identity-injector.edp-processing.svc.cluster.local:8080/api/ingress/v1/upload" \
        --from-literal=insights-url="http://identity-injector.edp-processing.svc.cluster.local:8080/api/v2" \
        -n openshift-config
    oc delete pod -n openshift-insights -l app=insights-operator
    oc wait --for=condition=ready pod -l app=insights-operator -n openshift-insights --timeout=60s
    echo "✓ Endpoint: http://identity-injector.edp-processing.svc.cluster.local:8080/api/ingress/v1/upload"
    echo "Run './verify-pipeline.sh' to verify archive processing"
}

configure_acm_client() {
    echo "=== ACM insights-client ==="
    oc get deployment insights-client -n open-cluster-management &>/dev/null || { echo "⚠️  ACM not installed"; return 0; }
    oc annotate multiclusterhub multiclusterhub -n open-cluster-management mch-pause=true --overwrite
    oc set env deployment/insights-client -n open-cluster-management \
        CCX_SERVER=http://identity-injector.edp-processing.svc.cluster.local:8080/api/v2
    oc rollout status deployment/insights-client -n open-cluster-management --timeout=120s
    echo "✓ ACM insights-client configured"
    echo "Run './verify-pipeline.sh' to verify query processing"
}

setup_all() {
    oc whoami &>/dev/null || { echo "❌ Not logged in"; return 1; }
    oc cluster-info | head -1
    confirm "Correct cluster?" || return 1
    setup_kafka && setup_databases && setup_edp_services && expose_services && configure_insights
    echo -e "\n✓ Setup complete"
    oc get pods -n kafka -n edp-processing
}

cleanup() {
    echo "WARNING: Deletes edp-processing, kafka namespaces, and insights config"
    read -p "Type 'yes': " C
    [ "$C" != "yes" ] && return 1
    oc get secret support -n openshift-config &>/dev/null && oc delete secret support -n openshift-config && \
        oc delete pod -n openshift-insights -l app=insights-operator 2>/dev/null || true
    for ns in edp-processing kafka; do oc delete ns $ns --wait=false 2>/dev/null; done
    oc wait --for=delete ns/edp-processing ns/kafka --timeout=120s 2>/dev/null || echo "⚠️  Deletion in progress"
    echo "✓ Cleanup complete"
}

restart_infra() {
    oc rollout restart deployment/{redis,mock-oauth2-server,rhobs-mock,identity-injector} -n edp-processing
    oc rollout restart statefulset/{postgresql,notification-db,minio} -n edp-processing
    echo "✓ Restarted"
}

verify() {
    FAILED=0
    echo "=== Kafka ==="
    check_pod kafka name=strimzi-cluster-operator "Strimzi"
    check_pod kafka strimzi.io/name=edp-kafka-kafka "Broker"
    oc get kafka edp-kafka -n kafka -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null | grep -q "True" && echo "✓ Cluster" || { echo "❌ Cluster"; FAILED=1; }

    echo -e "\n=== Infrastructure ==="
    for app in postgresql notification-db redis minio mock-oauth2-server rhobs-mock identity-injector; do
        check_pod edp-processing app=$app $app
    done

    echo -e "\n=== Processing ==="
    for app in ingress ccx-data-pipeline dvo-extractor db-writer dvo-writer cache-writer aggregator smart-proxy content-service ccx-upgrades-data-eng ccx-upgrades-inference notification-writer; do
        check_pod edp-processing app=$app $app
    done

    echo -e "\n=== Topics ==="
    T=$(oc exec -n kafka edp-kafka-dual-role-0 -- bin/kafka-topics.sh --bootstrap-server localhost:9092 --list 2>/dev/null)
    for topic in platform.upload.announce ccx.ocp.results; do
        echo "$T" | grep -q "$topic" && echo "✓ $topic" || { echo "❌ $topic"; FAILED=1; }
    done

    echo -e "\n=== Routes ==="
    for r in ingress aggregator smart-proxy; do
        oc get route $r -n edp-processing &>/dev/null && echo "✓ $r" || echo "⚠️  $r"
    done

    echo -e "\n=== Insights ==="
    oc get secret support -n openshift-config &>/dev/null && echo "✓ Configured" || echo "⚠️  Not configured"

    echo -e "\n=== Activity (3h) ==="
    U=$(oc logs -n edp-processing deployment/ingress --since=3h 2>/dev/null | grep -c "Payload received" 2>/dev/null || echo 0)
    P=$(oc logs -n edp-processing deployment/db-writer --since=3h 2>/dev/null | grep -c "processing message" 2>/dev/null || echo 0)
    [ "$U" -gt 0 ] 2>/dev/null && echo "✓ $U uploads" || echo "⚠️  No uploads"
    [ "$P" -gt 0 ] 2>/dev/null && echo "✓ $P processed" || echo "⚠️  No processing"

    echo -e "\n$([ $FAILED -eq 0 ] && echo '✓ PASSED' || echo '❌ FAILED')"
    return $FAILED
}

usage() {
    cat << EOF
Usage: $0 <command>

Setup Quay credentials first:
  oc create ns edp-processing
  oc create secret docker-registry quay-pull-secret --docker-server=quay.io \\
    --docker-username=<user> --docker-password=<pass> -n edp-processing
  oc secrets link default quay-pull-secret --for=pull -n edp-processing

Commands:
  all         Full installation
  kafka       Setup Kafka
  databases   Setup databases & infrastructure
  services    Deploy EDP services
  routes      Expose routes
  insights    Configure insights-operator
  acm-client  Configure ACM insights-client (optional)
  cleanup     Delete everything
  restart     Restart infrastructure
  verify      Health check

Pipeline Verification:
  ./verify-pipeline.sh  Verify archive upload and processing
EOF
}

case "${1:-}" in
    all) setup_all ;;
    kafka) setup_kafka ;;
    databases) setup_databases ;;
    services) setup_edp_services ;;
    routes) expose_services ;;
    insights) configure_insights ;;
    acm-client) configure_acm_client ;;
    cleanup) cleanup ;;
    restart) restart_infra ;;
    verify) verify ;;
    *) usage; exit 1 ;;
esac

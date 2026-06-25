import os


NAMESPACE = os.environ.get("NAMESPACE", "insights-on-prem")
DEPLOYMENT_NAME = os.environ.get("DEPLOYMENT_NAME", "insights-on-prem")


def test_deployment_exists(k8s_apps_client):
    deployment = k8s_apps_client.read_namespaced_deployment(
        name=DEPLOYMENT_NAME, namespace=NAMESPACE
    )
    assert deployment is not None


def test_deployment_has_ready_replicas(k8s_apps_client):
    deployment = k8s_apps_client.read_namespaced_deployment(
        name=DEPLOYMENT_NAME, namespace=NAMESPACE
    )
    assert deployment.status.ready_replicas is not None, (
        f"Deployment {DEPLOYMENT_NAME} has no ready replicas"
    )
    assert deployment.status.ready_replicas > 0


def test_pods_are_running(k8s_core_client):
    pods = k8s_core_client.list_namespaced_pod(
        namespace=NAMESPACE, label_selector=f"app={DEPLOYMENT_NAME}"
    )
    assert len(pods.items) > 0, (
        f"No pods found with label app={DEPLOYMENT_NAME} in namespace {NAMESPACE}"
    )
    for pod in pods.items:
        assert pod.status.phase == "Running", (
            f"Pod {pod.metadata.name} is {pod.status.phase}, expected Running"
        )

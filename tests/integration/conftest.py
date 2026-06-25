import pytest
from kubernetes import client, config


@pytest.fixture(scope="session")
def k8s_apps_client():
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.AppsV1Api()


@pytest.fixture(scope="session")
def k8s_core_client():
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.CoreV1Api()

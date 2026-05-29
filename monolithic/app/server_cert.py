"""Server certificate generator for mTLS with spoke clusters.

Generates a server certificate with the OpenShift route hostname as a SAN,
so HAProxy on spoke clusters can verify the server cert against the SNI
used for passthrough route matching.

Run as: python -m app.server_cert
"""

import logging
import os
import sys

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from kubernetes import client

from app.utils.kube_tls import (
    build_leaf_cert,
    ensure_ca_secret,
    get_pod_namespace,
    load_kube_config,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CA_SECRET_NAME = "insights-on-prem-server-ca"
CA_COMMON_NAME = "insights-on-prem-server-ca"
SERVER_CERT_VALIDITY_DAYS = 365
SERVICE_CA_CONFIGMAP = "insights-on-prem-service-ca"
SERVICE_NAME = "insights-on-prem"
ROUTE_NAME = "insights-on-prem"
TLS_DIR = "/tls"


def _get_route_hostname(namespace: str) -> str:
    custom = client.CustomObjectsApi()
    route = custom.get_namespaced_custom_object(
        group="route.openshift.io",
        version="v1",
        namespace=namespace,
        plural="routes",
        name=ROUTE_NAME,
    )
    hostname = route["spec"]["host"]
    logger.info("Route hostname: %s", hostname)
    return hostname


def _generate_server_cert(ca_key, ca_cert, sans: list[str]):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, sans[0])])
    cert = build_leaf_cert(
        subject=subject,
        public_key=key.public_key(),
        ca_key=ca_key,
        ca_cert=ca_cert,
        validity_days=SERVER_CERT_VALIDITY_DAYS,
        extended_key_usage=x509.oid.ExtendedKeyUsageOID.SERVER_AUTH,
        sans=[x509.DNSName(name) for name in sans],
    )
    return key, cert


def _write_cert_files(key, cert):
    os.makedirs(TLS_DIR, exist_ok=True)

    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)

    key_path = os.path.join(TLS_DIR, "tls.key")
    cert_path = os.path.join(TLS_DIR, "tls.crt")

    with open(key_path, "wb") as f:
        f.write(key_pem)
    with open(cert_path, "wb") as f:
        f.write(cert_pem)

    logger.info("Wrote server cert to %s and %s", cert_path, key_path)


def _ensure_service_ca_configmap(core_v1: client.CoreV1Api, namespace: str, ca_cert):
    """Create or update the ConfigMap that distributes the CA to spokes."""
    ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM).decode()

    cm = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(
            name=SERVICE_CA_CONFIGMAP,
            namespace=namespace,
        ),
        data={"ca-bundle.crt": ca_pem},
    )

    try:
        core_v1.create_namespaced_config_map(namespace, cm)
        logger.info("Created ConfigMap %s/%s", namespace, SERVICE_CA_CONFIGMAP)
    except client.ApiException as e:
        if e.status != 409:
            raise
        core_v1.replace_namespaced_config_map(SERVICE_CA_CONFIGMAP, namespace, cm)
        logger.info("Updated ConfigMap %s/%s", namespace, SERVICE_CA_CONFIGMAP)


def main():
    load_kube_config()
    namespace = get_pod_namespace()

    core_v1 = client.CoreV1Api()
    ca_key, ca_cert = ensure_ca_secret(
        core_v1, namespace, CA_SECRET_NAME, CA_COMMON_NAME
    )

    route_hostname = _get_route_hostname(namespace)

    sans = [
        f"{SERVICE_NAME}.{namespace}.svc",
        f"{SERVICE_NAME}.{namespace}.svc.cluster.local",
        route_hostname,
    ]
    logger.info("Generating server cert with SANs: %s", sans)

    server_key, server_cert = _generate_server_cert(ca_key, ca_cert, sans)
    _write_cert_files(server_key, server_cert)

    _ensure_service_ca_configmap(core_v1, namespace, ca_cert)

    logger.info("Server certificate setup complete")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Failed to generate server certificate")
        sys.exit(1)

"""Server certificate generator for mTLS with spoke clusters.

Generates a server certificate with the OpenShift route hostname as a SAN,
so HAProxy on spoke clusters can verify the server cert against the SNI
used for passthrough route matching.

Run as: python -m app.services.server_cert
"""

import base64
import datetime
import logging
import os
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from kubernetes import client, config

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CA_SECRET_NAME = "insights-on-prem-server-ca"
CA_CERT_VALIDITY_DAYS = 3650
SERVER_CERT_VALIDITY_DAYS = 365
SERVICE_CA_CONFIGMAP = "insights-on-prem-service-ca"
SERVICE_NAME = "insights-on-prem"
ROUTE_NAME = "insights-on-prem"
TLS_DIR = "/tls"

_NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"


def _get_pod_namespace() -> str:
    with open(_NAMESPACE_FILE) as f:
        return f.read().strip()


def _load_kube_config():
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


def _generate_ca():
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "insights-on-prem-server-ca"),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=CA_CERT_VALIDITY_DAYS)
        )
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _ensure_ca_secret(core_v1: client.CoreV1Api, namespace: str):
    """Create CA Secret if missing, return (ca_key, ca_cert).

    Uses optimistic concurrency: on 409 AlreadyExists, loads the
    winner's CA instead of failing.
    """
    try:
        secret = core_v1.read_namespaced_secret(CA_SECRET_NAME, namespace)
        ca_key = serialization.load_pem_private_key(
            base64.b64decode(secret.data["tls.key"]),
            password=None,
        )
        ca_cert = x509.load_pem_x509_certificate(
            base64.b64decode(secret.data["tls.crt"])
        )
        logger.info(
            "Loaded existing server CA from Secret %s/%s", namespace, CA_SECRET_NAME
        )
        return ca_key, ca_cert
    except client.ApiException as e:
        if e.status != 404:
            raise

    logger.info("Server CA Secret not found, generating new CA")
    ca_key, ca_cert = _generate_ca()

    key_pem = ca_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_pem = ca_cert.public_bytes(serialization.Encoding.PEM)

    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(name=CA_SECRET_NAME, namespace=namespace),
        type="kubernetes.io/tls",
        data={
            "tls.key": base64.b64encode(key_pem).decode(),
            "tls.crt": base64.b64encode(cert_pem).decode(),
        },
    )
    try:
        core_v1.create_namespaced_secret(namespace, secret)
        logger.info("Created server CA Secret %s/%s", namespace, CA_SECRET_NAME)
    except client.ApiException as e:
        if e.status != 409:
            raise
        logger.info("CA Secret created by another replica, loading it")
        return _ensure_ca_secret(core_v1, namespace)

    return ca_key, ca_cert


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
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, sans[0]),
        ]
    )

    san_entries = [x509.DNSName(name) for name in sans]

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_remaining = ca_cert.not_valid_after_utc - now
    validity = min(
        datetime.timedelta(days=SERVER_CERT_VALIDITY_DAYS),
        ca_remaining,
    )

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + validity)
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.SubjectAlternativeName(san_entries),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                content_commitment=False,
                key_cert_sign=False,
                crl_sign=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
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
    _load_kube_config()
    namespace = _get_pod_namespace()

    core_v1 = client.CoreV1Api()
    ca_key, ca_cert = _ensure_ca_secret(core_v1, namespace)

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

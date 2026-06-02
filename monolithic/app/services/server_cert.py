"""Server certificate management for mTLS with spoke clusters.

Generates and periodically renews the server certificate with the OpenShift
route hostname as a SAN, so HAProxy on spoke clusters can verify the server
cert against the SNI used for passthrough route matching.
"""

import asyncio
import datetime
import logging
import os
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from kubernetes import client

from app.config_loader import load_config
from app.services.csr_signer import CA_SECRET_NAME as CLIENT_CA_SECRET_NAME
from app.services.csr_signer import CA_COMMON_NAME as CLIENT_CA_COMMON_NAME
from app.services.csr_signer import update_client_ca
from app.utils.kube_tls import (
    build_leaf_cert,
    ensure_ca_secret,
    get_pod_namespace,
    load_ca_bundle_pem,
    load_kube_config,
    remove_expired_cas,
    renew_ca_secret,
)

logger = logging.getLogger(__name__)

CA_SECRET_NAME = "insights-on-prem-server-ca"
CA_COMMON_NAME = "insights-on-prem-server-ca"
SERVER_CERT_VALIDITY_DAYS = 365
RENEWAL_THRESHOLD_DAYS = 30
SERVICE_CA_CONFIGMAP = "insights-on-prem-service-ca"
SERVICE_NAME = "insights-on-prem"
ROUTE_NAME = "insights-on-prem"
TLS_DIR = "/tls"

_ssl_context: ssl.SSLContext | None = None


def set_ssl_context(ctx: ssl.SSLContext):
    """Store a reference to the running server's SSLContext for hot-reload."""
    global _ssl_context
    _ssl_context = ctx


def get_ssl_context() -> ssl.SSLContext | None:
    """Return the stored SSLContext, or None if not set yet."""
    return _ssl_context


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


def _load_cert_from_disk() -> x509.Certificate | None:
    """Load the server cert from disk, or return None if it doesn't exist."""
    cert_path = os.path.join(TLS_DIR, "tls.crt")
    if not os.path.exists(cert_path):
        return None
    with open(cert_path, "rb") as f:
        return x509.load_pem_x509_certificate(f.read())


def _ensure_service_ca_configmap(core_v1: client.CoreV1Api, namespace: str):
    """Create or update the ConfigMap that distributes the CA bundle to spokes."""
    ca_pem = load_ca_bundle_pem(core_v1, namespace, CA_SECRET_NAME)

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


def ensure_server_cert(ca_key, ca_cert, namespace: str):
    """Generate the server cert if missing or expiring, otherwise no-op."""
    cert = _load_cert_from_disk()

    if cert is not None:
        now = datetime.datetime.now(datetime.timezone.utc)
        remaining = cert.not_valid_after_utc - now
        if remaining > datetime.timedelta(days=RENEWAL_THRESHOLD_DAYS):
            logger.info(
                "Server cert valid for %d more days, no renewal needed",
                remaining.days,
            )
            return
        logger.info(
            "Server cert expires in %d days, renewing",
            remaining.days,
        )
    else:
        logger.info("No server cert found, generating")

    core_v1 = client.CoreV1Api()
    route_hostname = _get_route_hostname(namespace)

    sans = [
        f"{SERVICE_NAME}.{namespace}.svc",
        f"{SERVICE_NAME}.{namespace}.svc.cluster.local",
        route_hostname,
    ]
    logger.info("Generating server cert with SANs: %s", sans)

    server_key, server_cert = _generate_server_cert(ca_key, ca_cert, sans)
    _write_cert_files(server_key, server_cert)

    ctx = get_ssl_context()
    if ctx is not None:
        cert_path = os.path.join(TLS_DIR, "tls.crt")
        key_path = os.path.join(TLS_DIR, "tls.key")
        ctx.load_cert_chain(cert_path, key_path)
        logger.info("Reloaded SSLContext with new server cert")

    _ensure_service_ca_configmap(core_v1, namespace)

    logger.info("Server certificate setup complete")


def _renew_all_certs(core_v1, namespace):
    """Check and renew CAs and server cert in a single pass."""
    ca_key, ca_cert = renew_ca_secret(
        core_v1, namespace, CA_SECRET_NAME, CA_COMMON_NAME
    )

    client_ca_key, client_ca_cert = renew_ca_secret(
        core_v1, namespace, CLIENT_CA_SECRET_NAME, CLIENT_CA_COMMON_NAME
    )
    update_client_ca(client_ca_key, client_ca_cert)

    remove_expired_cas(core_v1, namespace, CA_SECRET_NAME)
    remove_expired_cas(core_v1, namespace, CLIENT_CA_SECRET_NAME)

    ensure_server_cert(ca_key, ca_cert, namespace)


async def run_cert_renewal():
    """Background task that periodically checks and renews CAs and server cert."""
    config = load_config()
    interval_hours = config.cert_renewal_check_interval_hours

    load_kube_config()
    namespace = get_pod_namespace()

    core_v1 = client.CoreV1Api()
    ensure_ca_secret(core_v1, namespace, CA_SECRET_NAME, CA_COMMON_NAME)
    ensure_ca_secret(core_v1, namespace, CLIENT_CA_SECRET_NAME, CLIENT_CA_COMMON_NAME)

    while True:
        try:
            await asyncio.to_thread(_renew_all_certs, core_v1, namespace)
        except Exception:
            logger.exception("Error during cert renewal check")

        await asyncio.sleep(interval_hours * 3600)

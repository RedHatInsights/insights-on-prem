"""TLS certificate lifecycle management.

Manages server and client CA certificates, server TLS certificates,
and the CA bundle ConfigMap distributed to spoke clusters. A background
task periodically renews CAs (with certificate chaining for zero-downtime
rotation), regenerates the server cert, and hot-reloads the SSLContext.
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
from app.utils.kube_tls import (
    CLIENT_CA_NAME,
    SERVER_CA_NAME,
    build_leaf_cert,
    ensure_ca_secret,
    load_ca_bundle_pem,
    remove_expired_cas,
    renew_ca_secret,
)

logger = logging.getLogger(__name__)
SERVER_CERT_VALIDITY_DAYS = 365
RENEWAL_THRESHOLD_DAYS = 30
SERVICE_CA_CONFIGMAP = "insights-on-prem-service-ca"
SERVICE_NAME = "insights-on-prem"
ROUTE_NAME = "insights-on-prem"
TLS_DIR = "/tls"
CLIENT_CA_PATH = os.path.join(TLS_DIR, "client-ca.crt")


class TLSManager:
    """Manages server and client TLS certificates for the mTLS deployment.

    Created in start_server() before uvicorn starts, stored on app.state.
    Background renewal task is started in the FastAPI lifespan.
    """

    def __init__(self, core_v1: client.CoreV1Api, namespace: str):
        self.core_v1 = core_v1
        self.namespace = namespace
        self.ssl_context: ssl.SSLContext | None = None
        self.server_ca_key = None
        self.server_ca_cert = None
        self.client_ca_key = None
        self.client_ca_cert = None

    def ensure_ca_secrets(self):
        """Create or load both server and client CA secrets."""
        self.server_ca_key, self.server_ca_cert = ensure_ca_secret(
            self.core_v1, self.namespace, SERVER_CA_NAME, SERVER_CA_NAME
        )
        self.client_ca_key, self.client_ca_cert = ensure_ca_secret(
            self.core_v1, self.namespace, CLIENT_CA_NAME, CLIENT_CA_NAME
        )

    def _get_route_hostname(self) -> str:
        custom = client.CustomObjectsApi()
        route = custom.get_namespaced_custom_object(
            group="route.openshift.io",
            version="v1",
            namespace=self.namespace,
            plural="routes",
            name=ROUTE_NAME,
        )
        hostname = route["spec"]["host"]
        logger.info("Route hostname: %s", hostname)
        return hostname

    def _generate_server_cert(self, sans: list[str]):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, sans[0])])
        cert = build_leaf_cert(
            subject=subject,
            public_key=key.public_key(),
            ca_key=self.server_ca_key,
            ca_cert=self.server_ca_cert,
            validity_days=SERVER_CERT_VALIDITY_DAYS,
            extended_key_usage=x509.oid.ExtendedKeyUsageOID.SERVER_AUTH,
            sans=[x509.DNSName(name) for name in sans],
        )
        return key, cert

    @staticmethod
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

    @staticmethod
    def _load_cert_from_disk() -> x509.Certificate | None:
        cert_path = os.path.join(TLS_DIR, "tls.crt")
        if not os.path.exists(cert_path):
            return None
        with open(cert_path, "rb") as f:
            return x509.load_pem_x509_certificate(f.read())

    def _ensure_service_ca_configmap(self):
        ca_pem = load_ca_bundle_pem(self.core_v1, self.namespace, SERVER_CA_NAME)
        cm = client.V1ConfigMap(
            metadata=client.V1ObjectMeta(
                name=SERVICE_CA_CONFIGMAP,
                namespace=self.namespace,
            ),
            data={"ca-bundle.crt": ca_pem},
        )
        try:
            self.core_v1.create_namespaced_config_map(self.namespace, cm)
            logger.info("Created ConfigMap %s/%s", self.namespace, SERVICE_CA_CONFIGMAP)
        except client.ApiException as e:
            if e.status != 409:
                raise
            existing = self.core_v1.read_namespaced_config_map(
                SERVICE_CA_CONFIGMAP, self.namespace
            )
            cm.metadata.resource_version = existing.metadata.resource_version
            self.core_v1.replace_namespaced_config_map(
                SERVICE_CA_CONFIGMAP, self.namespace, cm
            )
            logger.info("Updated ConfigMap %s/%s", self.namespace, SERVICE_CA_CONFIGMAP)

    def ensure_server_cert(self, force=False):
        """Generate the server cert if missing, expiring, or forced by CA rotation."""
        cert = self._load_cert_from_disk()
        if cert is not None and not force:
            now = datetime.datetime.now(datetime.timezone.utc)
            remaining = cert.not_valid_after_utc - now
            if remaining > datetime.timedelta(days=RENEWAL_THRESHOLD_DAYS):
                logger.info(
                    "Server cert valid for %d more days, no renewal needed",
                    remaining.days,
                )
                return
            logger.info("Server cert expires in %d days, renewing", remaining.days)
        elif cert is None:
            logger.info("No server cert found, generating")
        else:
            logger.info("Forced server cert renewal (CA rotated)")

        route_hostname = self._get_route_hostname()
        sans = [
            f"{SERVICE_NAME}.{self.namespace}.svc",
            f"{SERVICE_NAME}.{self.namespace}.svc.cluster.local",
            route_hostname,
        ]
        logger.info("Generating server cert with SANs: %s", sans)

        server_key, server_cert = self._generate_server_cert(sans)
        self._write_cert_files(server_key, server_cert)

        if self.ssl_context is not None:
            cert_path = os.path.join(TLS_DIR, "tls.crt")
            key_path = os.path.join(TLS_DIR, "tls.key")
            self.ssl_context.load_cert_chain(cert_path, key_path)
            logger.info("Reloaded SSLContext with new server cert")

        self._ensure_service_ca_configmap()
        logger.info("Server certificate setup complete")

    def write_client_ca_bundle(self):
        """Write the current client CA bundle to disk and reload on the SSLContext."""
        ca_pem = load_ca_bundle_pem(self.core_v1, self.namespace, CLIENT_CA_NAME)
        with open(CLIENT_CA_PATH, "w") as f:
            f.write(ca_pem)
        if self.ssl_context is not None:
            self.ssl_context.load_verify_locations(cafile=CLIENT_CA_PATH)
            logger.info("Reloaded SSLContext with updated client CA bundle")

    def _renew_all_certs(self):
        old_server_ca_cert = self.server_ca_cert
        self.server_ca_key, self.server_ca_cert = renew_ca_secret(
            self.core_v1, self.namespace, SERVER_CA_NAME, SERVER_CA_NAME
        )
        self.client_ca_key, self.client_ca_cert = renew_ca_secret(
            self.core_v1, self.namespace, CLIENT_CA_NAME, CLIENT_CA_NAME
        )
        remove_expired_cas(self.core_v1, self.namespace, SERVER_CA_NAME)
        remove_expired_cas(self.core_v1, self.namespace, CLIENT_CA_NAME)
        self.write_client_ca_bundle()
        server_ca_rotated = self.server_ca_cert.serial_number != old_server_ca_cert.serial_number
        self.ensure_server_cert(force=server_ca_rotated)

    async def run_renewal(self):
        """Background task that periodically checks and renews CAs and server cert."""
        config = load_config()
        interval_hours = config.cert_renewal_check_interval_hours
        while True:
            try:
                await asyncio.to_thread(self._renew_all_certs)
            except Exception:
                logger.exception("Error during cert renewal check")
            await asyncio.sleep(interval_hours * 3600)

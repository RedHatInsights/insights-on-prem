"""CSR watcher and signer for mTLS authentication with spoke clusters.

Watches CertificateSigningRequest resources on the hub cluster and signs
them with a local CA. The OCM registration agent on each spoke generates
keypairs and submits CSRs; this service approves and signs them so the
agent can deliver the signed cert to the spoke.
"""

import base64
import datetime
import logging

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from kubernetes import client, config, watch

logger = logging.getLogger(__name__)

SIGNER_NAME = "open-cluster-management.io/insights-on-prem-signer"
CA_SECRET_NAME = "insights-operator-proxy-ca"
CA_CERT_VALIDITY_DAYS = 3650
CLIENT_CERT_VALIDITY_DAYS = 365
ALLOWED_USERNAME_PREFIX = "system:open-cluster-management:"
_NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"


def _get_pod_namespace() -> str:
    """Read the pod's namespace from the service account mount."""
    with open(_NAMESPACE_FILE) as f:
        return f.read().strip()


def _load_kube_clients():
    """Load kubernetes clients, preferring in-cluster config."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.CoreV1Api(), client.CertificatesV1Api()


def _generate_ca():
    """Generate a self-signed CA keypair."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "insights-on-prem-ca"),
    ])
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


def ensure_ca_secret(namespace: str):
    """Create CA Secret if it doesn't exist, return (ca_key, ca_cert)."""
    core_v1, _ = _load_kube_clients()

    try:
        secret = core_v1.read_namespaced_secret(CA_SECRET_NAME, namespace)
        ca_key = serialization.load_pem_private_key(
            base64.b64decode(secret.data["tls.key"]),
            password=None,
        )
        ca_cert = x509.load_pem_x509_certificate(
            base64.b64decode(secret.data["tls.crt"])
        )
        logger.info("Loaded existing CA from Secret %s/%s", namespace, CA_SECRET_NAME)
        return ca_key, ca_cert
    except client.ApiException as e:
        if e.status != 404:
            raise

    logger.info("CA Secret not found, generating new CA")
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
    core_v1.create_namespaced_secret(namespace, secret)
    logger.info("Created CA Secret %s/%s", namespace, CA_SECRET_NAME)
    return ca_key, ca_cert


def _sign_csr(csr_pem: bytes, ca_key, ca_cert) -> bytes:
    """Sign a CSR with the CA and return the signed certificate PEM."""
    csr = x509.load_pem_x509_csr(csr_pem)

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_remaining = ca_cert.not_valid_after_utc - now
    validity = min(
        datetime.timedelta(days=CLIENT_CERT_VALIDITY_DAYS),
        ca_remaining,
    )

    cert = (
        x509.CertificateBuilder()
        .subject_name(csr.subject)
        .issuer_name(ca_cert.subject)
        .public_key(csr.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + validity)
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
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
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def _approve_csr(certs_v1: client.CertificatesV1Api, csr_name: str):
    """Approve a CertificateSigningRequest."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat() + "Z"
    certs_v1.patch_certificate_signing_request_approval(
        csr_name,
        body={
            "status": {
                "conditions": [
                    {
                        "type": "Approved",
                        "status": "True",
                        "reason": "InsightsOnPremApproved",
                        "message": "Approved by insights-on-prem CSR signer",
                        "lastUpdateTime": now,
                    }
                ]
            }
        },
    )


async def run_csr_watcher():
    """Start the CSR watcher in a background thread.

    The kubernetes watch API is synchronous and blocks on HTTP long-poll,
    so it must run in a thread to avoid freezing the asyncio event loop.
    """
    import asyncio

    await asyncio.to_thread(_csr_watch_loop)


def _csr_watch_loop():
    """Blocking CSR watch loop — runs in a thread via asyncio.to_thread."""
    import time

    namespace = _get_pod_namespace()
    ca_key, ca_cert = ensure_ca_secret(namespace)
    _, certs_v1 = _load_kube_clients()

    logger.info("Starting CSR watcher for signer %s", SIGNER_NAME)

    while True:
        try:
            w = watch.Watch()
            for event in w.stream(
                certs_v1.list_certificate_signing_request,
                field_selector=f"spec.signerName={SIGNER_NAME}",
                timeout_seconds=300,
            ):
                csr_obj = event["object"]
                event_type = event["type"]

                if event_type not in ("ADDED", "MODIFIED"):
                    continue

                csr_name = csr_obj.metadata.name

                if csr_obj.status and csr_obj.status.certificate:
                    continue

                if csr_obj.status and csr_obj.status.conditions:
                    conditions = {c.type for c in csr_obj.status.conditions}
                    if "Approved" in conditions:
                        continue

                username = csr_obj.spec.username or ""
                if not username.startswith(ALLOWED_USERNAME_PREFIX):
                    logger.warning(
                        "Rejecting CSR %s: username %s does not start with %s",
                        csr_name,
                        username,
                        ALLOWED_USERNAME_PREFIX,
                    )
                    continue

                logger.info("Approving and signing CSR %s from %s", csr_name, username)

                _approve_csr(certs_v1, csr_name)

                csr_pem = base64.b64decode(csr_obj.spec.request)
                signed_cert_pem = _sign_csr(csr_pem, ca_key, ca_cert)

                certs_v1.patch_certificate_signing_request_status(
                    csr_name,
                    body={
                        "status": {
                            "certificate": base64.b64encode(signed_cert_pem).decode()
                        }
                    },
                )

                logger.info("Signed CSR %s successfully", csr_name)

        except client.ApiException as e:
            logger.error("Kubernetes API error in CSR watcher: %s", e)
            time.sleep(10)
        except Exception:
            logger.exception("Unexpected error in CSR watcher")
            time.sleep(10)

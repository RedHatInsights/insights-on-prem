"""Shared Kubernetes TLS/CA utilities.

Provides helpers for loading kube config, reading the pod namespace,
generating self-signed CAs, and managing CA secrets — used by both
the CSR signer and the server certificate generator.
"""

import base64
import datetime
import logging

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from kubernetes import client, config

logger = logging.getLogger(__name__)

CA_CERT_VALIDITY_DAYS = 3650
_NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"


def get_pod_namespace() -> str:
    """Read the pod's namespace from the service account mount."""
    with open(_NAMESPACE_FILE) as f:
        return f.read().strip()


def load_kube_config():
    """Load kubernetes config, preferring in-cluster."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


def generate_ca(common_name: str) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    """Generate a self-signed CA keypair with the given CN."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, common_name)]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=CA_CERT_VALIDITY_DAYS))
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


def build_leaf_cert(
    subject: x509.Name,
    public_key,
    ca_key,
    ca_cert,
    validity_days: int,
    extended_key_usage: x509.ObjectIdentifier,
    sans: list[x509.GeneralName] | None = None,
) -> x509.Certificate:
    """Build and sign a leaf (non-CA) certificate."""
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_remaining = ca_cert.not_valid_after_utc - now
    validity = min(datetime.timedelta(days=validity_days), ca_remaining)

    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(public_key)
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
            x509.ExtendedKeyUsage([extended_key_usage]),
            critical=False,
        )
    )
    if sans:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(sans),
            critical=False,
        )
    return builder.sign(ca_key, hashes.SHA256())


def ensure_ca_secret(
    core_v1: client.CoreV1Api,
    namespace: str,
    secret_name: str,
    common_name: str,
) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    """Create a CA Secret if it doesn't exist, return (ca_key, ca_cert).

    Uses optimistic concurrency: on 409 AlreadyExists, loads the
    winner's CA instead of failing.
    """
    try:
        secret = core_v1.read_namespaced_secret(secret_name, namespace)
        ca_key = serialization.load_pem_private_key(
            base64.b64decode(secret.data["tls.key"]),
            password=None,
        )
        ca_cert = x509.load_pem_x509_certificate(
            base64.b64decode(secret.data["tls.crt"])
        )
        logger.info("Loaded existing CA from Secret %s/%s", namespace, secret_name)
        return ca_key, ca_cert
    except client.ApiException as e:
        if e.status != 404:
            raise

    logger.info("CA Secret %s not found, generating new CA", secret_name)
    ca_key, ca_cert = generate_ca(common_name)

    key_pem = ca_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_pem = ca_cert.public_bytes(serialization.Encoding.PEM)

    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(name=secret_name, namespace=namespace),
        type="kubernetes.io/tls",
        data={
            "tls.key": base64.b64encode(key_pem).decode(),
            "tls.crt": base64.b64encode(cert_pem).decode(),
        },
    )
    try:
        core_v1.create_namespaced_secret(namespace, secret)
        logger.info("Created CA Secret %s/%s", namespace, secret_name)
    except client.ApiException as e:
        if e.status != 409:
            raise
        logger.info("CA Secret created by another replica, loading it")
        return ensure_ca_secret(core_v1, namespace, secret_name, common_name)

    return ca_key, ca_cert

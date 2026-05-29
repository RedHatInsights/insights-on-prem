"""CSR watcher and signer for mTLS authentication with spoke clusters.

Watches CertificateSigningRequest resources on the hub cluster and signs
them with a local CA. The OCM registration agent on each spoke generates
keypairs and submits CSRs; this service approves and signs them so the
agent can deliver the signed cert to the spoke.
"""

import asyncio
import base64
import datetime
import logging
import time

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from kubernetes import client, watch

from app.utils.kube_tls import (
    build_leaf_cert,
    ensure_ca_secret,
    get_pod_namespace,
    load_kube_config,
)

logger = logging.getLogger(__name__)

SIGNER_NAME = "open-cluster-management.io/insights-on-prem-signer"
CA_SECRET_NAME = "insights-operator-proxy-ca"
CA_COMMON_NAME = "insights-on-prem-ca"
CLIENT_CERT_VALIDITY_DAYS = 365
ALLOWED_USERNAME_PREFIX = "system:open-cluster-management:"


def _sign_csr(csr_pem: bytes, ca_key, ca_cert) -> bytes:
    """Sign a CSR with the CA and return the signed certificate PEM."""
    csr = x509.load_pem_x509_csr(csr_pem)
    cert = build_leaf_cert(
        subject=csr.subject,
        public_key=csr.public_key(),
        ca_key=ca_key,
        ca_cert=ca_cert,
        validity_days=CLIENT_CERT_VALIDITY_DAYS,
        extended_key_usage=x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH,
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
    await asyncio.to_thread(_csr_watch_loop)


def _should_process(event) -> str | None:
    """Return the requester's username if this CSR needs signing, None to skip."""
    if event["type"] not in ("ADDED", "MODIFIED"):
        return None

    csr_obj = event["object"]

    if csr_obj.status and csr_obj.status.certificate:
        return None

    if (
        csr_obj.status
        and csr_obj.status.conditions
        and "Approved" in {c.type for c in csr_obj.status.conditions}
    ):
        return None

    username = csr_obj.spec.username or ""
    if not username.startswith(ALLOWED_USERNAME_PREFIX):
        logger.warning(
            "Rejecting CSR %s: username %s does not start with %s",
            csr_obj.metadata.name,
            username,
            ALLOWED_USERNAME_PREFIX,
        )
        return None

    return username


def _process_csr(certs_v1, csr_obj, ca_key, ca_cert):
    """Approve a CSR, sign it with the CA, and upload the signed certificate."""
    csr_name = csr_obj.metadata.name

    _approve_csr(certs_v1, csr_name)

    csr_pem = base64.b64decode(csr_obj.spec.request)
    signed_cert_pem = _sign_csr(csr_pem, ca_key, ca_cert)

    certs_v1.patch_certificate_signing_request_status(
        csr_name,
        body={"status": {"certificate": base64.b64encode(signed_cert_pem).decode()}},
    )

    logger.info("Signed CSR %s successfully", csr_name)


def _csr_watch_loop():
    """Blocking CSR watch loop — runs in a thread via asyncio.to_thread."""
    load_kube_config()
    namespace = get_pod_namespace()

    core_v1 = client.CoreV1Api()
    certs_v1 = client.CertificatesV1Api()

    ca_key, ca_cert = ensure_ca_secret(
        core_v1, namespace, CA_SECRET_NAME, CA_COMMON_NAME
    )

    logger.info("Starting CSR watcher for signer %s", SIGNER_NAME)

    while True:
        try:
            w = watch.Watch()
            for event in w.stream(
                certs_v1.list_certificate_signing_request,
                field_selector=f"spec.signerName={SIGNER_NAME}",
                timeout_seconds=300,
            ):
                username = _should_process(event)
                if not username:
                    continue

                csr_obj = event["object"]
                logger.info(
                    "Approving and signing CSR %s from %s",
                    csr_obj.metadata.name,
                    username,
                )
                _process_csr(certs_v1, csr_obj, ca_key, ca_cert)

        except client.ApiException as e:
            logger.error("Kubernetes API error in CSR watcher: %s", e)
            time.sleep(10)
        except Exception:
            logger.exception("Unexpected error in CSR watcher")
            time.sleep(10)

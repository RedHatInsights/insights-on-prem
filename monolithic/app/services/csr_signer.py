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

from app.utils.kube_tls import build_leaf_cert

logger = logging.getLogger(__name__)

SIGNER_NAME = "open-cluster-management.io/insights-on-prem-signer"
CLIENT_CERT_VALIDITY_DAYS = 365
ALLOWED_USERNAME_PREFIX = "system:open-cluster-management:"


class CSRSigner:
    """Watches and signs CertificateSigningRequests from spoke clusters.

    Reads the client CA key and cert from the TLSManager instance,
    which keeps them up to date through its renewal loop.
    """

    def __init__(self, tls_manager):
        self.tls_manager = tls_manager

    @staticmethod
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
            and {"Approved", "Denied", "Failed"}
            & {c.type for c in csr_obj.status.conditions}
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

    def _sign_csr(self, csr_pem: bytes) -> bytes:
        csr = x509.load_pem_x509_csr(csr_pem)
        cert = build_leaf_cert(
            subject=csr.subject,
            public_key=csr.public_key(),
            ca_key=self.tls_manager.client_ca_key,
            ca_cert=self.tls_manager.client_ca_cert,
            validity_days=CLIENT_CERT_VALIDITY_DAYS,
            extended_key_usage=x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH,
        )
        return cert.public_bytes(serialization.Encoding.PEM)

    @staticmethod
    def _approve_csr(certs_v1: client.CertificatesV1Api, csr_name: str):
        now = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
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

    def _process_csr(self, certs_v1, csr_obj):
        csr_name = csr_obj.metadata.name
        csr_pem = base64.b64decode(csr_obj.spec.request)
        signed_cert_pem = self._sign_csr(csr_pem)
        self._approve_csr(certs_v1, csr_name)
        certs_v1.patch_certificate_signing_request_status(
            csr_name,
            body={
                "status": {"certificate": base64.b64encode(signed_cert_pem).decode()}
            },
        )
        logger.info("Signed CSR %s successfully", csr_name)

    def _watch_loop(self):
        certs_v1 = client.CertificatesV1Api()
        logger.info("Starting CSR watcher for signer %s", SIGNER_NAME)
        while True:
            try:
                w = watch.Watch()
                for event in w.stream(
                    certs_v1.list_certificate_signing_request,
                    field_selector=f"spec.signerName={SIGNER_NAME}",
                    timeout_seconds=300,
                ):
                    username = self._should_process(event)
                    if not username:
                        continue
                    csr_obj = event["object"]
                    logger.info(
                        "Approving and signing CSR %s from %s",
                        csr_obj.metadata.name,
                        username,
                    )
                    self._process_csr(certs_v1, csr_obj)
            except client.ApiException as e:
                logger.error("Kubernetes API error in CSR watcher: %s", e)
                time.sleep(10)
            except Exception:
                logger.exception("Unexpected error in CSR watcher")
                time.sleep(10)

    async def run(self):
        """Start the CSR watcher in a background thread."""
        await asyncio.to_thread(self._watch_loop)

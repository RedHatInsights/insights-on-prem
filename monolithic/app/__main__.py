"""Programmatic uvicorn entry point with managed SSLContext.

Usage: python -m app

Generates the initial server certificate, starts uvicorn with SSL,
and stores a reference to the SSLContext so the background cert
renewal task can hot-reload certificates without restarting.
"""

import logging
import os
import ssl

import uvicorn
from kubernetes import client

from app.services.server_cert import (
    CA_COMMON_NAME,
    CA_SECRET_NAME,
    ensure_server_cert,
    set_ssl_context,
)
from app.utils.kube_tls import ensure_ca_secret, get_pod_namespace, load_kube_config

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TLS_DIR = "/tls"
MTLS_CA_PATH = "/mtls-ca/tls.crt"


def main():
    load_kube_config()
    namespace = get_pod_namespace()

    core_v1 = client.CoreV1Api()
    ca_key, ca_cert = ensure_ca_secret(
        core_v1, namespace, CA_SECRET_NAME, CA_COMMON_NAME
    )

    ensure_server_cert(ca_key, ca_cert, namespace)

    ssl_kwargs: dict = {
        "ssl_keyfile": os.path.join(TLS_DIR, "tls.key"),
        "ssl_certfile": os.path.join(TLS_DIR, "tls.crt"),
    }

    mtls_enabled = os.environ.get("MTLS_ENABLED", "").lower() == "true"
    if mtls_enabled and os.path.isfile(MTLS_CA_PATH):
        ssl_kwargs["ssl_ca_certs"] = MTLS_CA_PATH
        ssl_kwargs["ssl_cert_reqs"] = ssl.CERT_REQUIRED
        logger.info("mTLS client verification enabled")

    config = uvicorn.Config("app.main:app", host="0.0.0.0", port=8443, **ssl_kwargs)
    config.load()

    set_ssl_context(config.ssl)
    logger.info("SSLContext stored for hot-reload")

    server = uvicorn.Server(config)
    server.run()


if __name__ == "__main__":
    main()

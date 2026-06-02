"""Tests for app.services.tls_manager."""

import base64
import datetime
import os
import ssl
from unittest.mock import MagicMock, patch

from app.services.tls_manager import TLSManager
from app.utils.kube_tls import generate_ca
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from kubernetes import client


def _make_tls_manager(core_v1=None, namespace="test-ns"):
    """Create a TLSManager with a mock CoreV1Api and test CA keys."""
    if core_v1 is None:
        core_v1 = MagicMock(spec=client.CoreV1Api)
    mgr = TLSManager(core_v1, namespace)
    ca_key, ca_cert = generate_ca("test-server-ca")
    mgr.server_ca_key = ca_key
    mgr.server_ca_cert = ca_cert
    return mgr


def _mock_core_with_ca_secret():
    """Helper: create a mock CoreV1Api with a CA secret containing two chained certs."""
    _, cert1 = generate_ca("ca-one")
    _, cert2 = generate_ca("ca-two")
    chain_pem = cert1.public_bytes(serialization.Encoding.PEM) + cert2.public_bytes(
        serialization.Encoding.PEM
    )
    secret_data = {"tls.crt": base64.b64encode(chain_pem).decode()}
    mock_core = MagicMock(spec=client.CoreV1Api)
    mock_core.read_namespaced_secret.return_value = MagicMock(data=secret_data)
    return mock_core, chain_pem


def _write_test_cert(tmp_path, ca_key, ca_cert, validity_days):
    """Helper: generate a server cert with given validity and write to tmp_path."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=validity_days))
        .sign(ca_key, hashes.SHA256())
    )
    cert_path = os.path.join(str(tmp_path), "tls.crt")
    key_path = os.path.join(str(tmp_path), "tls.key")
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )


@patch("app.services.tls_manager.TLSManager._ensure_service_ca_configmap")
@patch(
    "app.services.tls_manager.TLSManager._get_route_hostname",
    return_value="route.example.com",
)
def test_ensure_server_cert_generates_when_missing(
    _mock_route, _mock_configmap, tmp_path, monkeypatch
):
    """Verify ensure_server_cert generates a cert when none exists on disk."""
    monkeypatch.setattr("app.services.tls_manager.TLS_DIR", str(tmp_path))

    mgr = _make_tls_manager()
    mgr.ensure_server_cert()

    assert os.path.exists(os.path.join(str(tmp_path), "tls.crt"))
    assert os.path.exists(os.path.join(str(tmp_path), "tls.key"))


@patch("app.services.tls_manager.TLSManager._ensure_service_ca_configmap")
@patch(
    "app.services.tls_manager.TLSManager._get_route_hostname",
    return_value="route.example.com",
)
def test_ensure_server_cert_renews_when_expiring(
    _mock_route, _mock_configmap, tmp_path, monkeypatch
):
    """Verify ensure_server_cert renews a cert expiring within the threshold."""
    monkeypatch.setattr("app.services.tls_manager.TLS_DIR", str(tmp_path))

    mgr = _make_tls_manager()
    _write_test_cert(tmp_path, mgr.server_ca_key, mgr.server_ca_cert, validity_days=20)

    mock_ctx = MagicMock(spec=ssl.SSLContext)
    mgr.ssl_context = mock_ctx

    old_cert_path = os.path.join(str(tmp_path), "tls.crt")
    with open(old_cert_path, "rb") as f:
        old_cert = x509.load_pem_x509_certificate(f.read())

    mgr.ensure_server_cert()

    with open(old_cert_path, "rb") as f:
        new_cert = x509.load_pem_x509_certificate(f.read())
    assert new_cert.serial_number != old_cert.serial_number

    mock_ctx.load_cert_chain.assert_called_once()


@patch("app.services.tls_manager.TLSManager._ensure_service_ca_configmap")
@patch(
    "app.services.tls_manager.TLSManager._get_route_hostname",
    return_value="route.example.com",
)
def test_ensure_server_cert_noop_when_valid(
    mock_route, _mock_configmap, tmp_path, monkeypatch
):
    """Verify ensure_server_cert is a no-op when cert has plenty of validity left."""
    monkeypatch.setattr("app.services.tls_manager.TLS_DIR", str(tmp_path))

    mgr = _make_tls_manager()
    _write_test_cert(tmp_path, mgr.server_ca_key, mgr.server_ca_cert, validity_days=300)

    cert_path = os.path.join(str(tmp_path), "tls.crt")
    with open(cert_path, "rb") as f:
        original_cert = x509.load_pem_x509_certificate(f.read())

    mgr.ensure_server_cert()

    with open(cert_path, "rb") as f:
        after_cert = x509.load_pem_x509_certificate(f.read())
    assert after_cert.serial_number == original_cert.serial_number

    mock_route.assert_not_called()


def test_write_client_ca_bundle_writes_and_reloads_ctx(tmp_path, monkeypatch):
    """Verify write_client_ca_bundle writes the CA bundle and reloads the SSLContext."""
    ca_path = str(tmp_path / "client-ca.crt")
    monkeypatch.setattr("app.services.tls_manager.CLIENT_CA_PATH", ca_path)

    mock_core, chain_pem = _mock_core_with_ca_secret()
    mgr = _make_tls_manager(core_v1=mock_core)

    mock_ctx = MagicMock(spec=ssl.SSLContext)
    mgr.ssl_context = mock_ctx

    mgr.write_client_ca_bundle()

    with open(ca_path) as f:
        written = f.read()
    assert written.count("BEGIN CERTIFICATE") == 2

    mock_ctx.load_verify_locations.assert_called_once_with(cafile=ca_path)

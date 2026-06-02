"""Tests for app.services.server_cert."""

import base64
import datetime
import os
import ssl
from unittest.mock import MagicMock, patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from kubernetes import client

from app.services.server_cert import (
    _ensure_service_ca_configmap,
    _generate_server_cert,
    _write_cert_files,
    ensure_server_cert,
    get_ssl_context,
    set_ssl_context,
)
from app.utils.kube_tls import generate_ca


def test_generate_server_cert():
    """Verify _generate_server_cert produces a server cert with correct SANs and CN."""
    ca_key, ca_cert = generate_ca("test-server-ca")
    sans = ["svc.local", "svc.cluster.local", "route.example.com"]

    key, cert = _generate_server_cert(ca_key, ca_cert, sans)

    assert (
        cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "svc.local"
    )
    assert cert.issuer == ca_cert.subject

    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert x509.oid.ExtendedKeyUsageOID.SERVER_AUTH in eku

    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    dns_names = san.get_values_for_type(x509.DNSName)
    assert set(dns_names) == set(sans)


def test_write_cert_files(tmp_path, monkeypatch):
    """Verify _write_cert_files writes valid PEM files to disk."""
    monkeypatch.setattr("app.services.server_cert.TLS_DIR", str(tmp_path))

    ca_key, ca_cert = generate_ca("test-ca")

    key, cert = _generate_server_cert(ca_key, ca_cert, ["test.local"])
    _write_cert_files(key, cert)

    key_path = os.path.join(str(tmp_path), "tls.key")
    cert_path = os.path.join(str(tmp_path), "tls.crt")

    assert os.path.exists(key_path)
    assert os.path.exists(cert_path)

    with open(cert_path, "rb") as f:
        loaded_cert = x509.load_pem_x509_certificate(f.read())
        assert loaded_cert.subject == cert.subject

    with open(key_path, "rb") as f:
        loaded_key = serialization.load_pem_private_key(f.read(), password=None)
        assert loaded_key.key_size == key.key_size


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


def test_ensure_service_ca_configmap_creates():
    """Verify _ensure_service_ca_configmap creates a ConfigMap with full CA bundle."""
    mock_core, chain_pem = _mock_core_with_ca_secret()

    _ensure_service_ca_configmap(mock_core, "test-ns")

    mock_core.create_namespaced_config_map.assert_called_once()
    call_args = mock_core.create_namespaced_config_map.call_args
    cm = call_args[0][1]
    assert cm.data["ca-bundle.crt"].count("BEGIN CERTIFICATE") == 2


def test_ensure_service_ca_configmap_updates_on_conflict():
    """Verify _ensure_service_ca_configmap falls back to replace on 409 conflict."""
    mock_core, _ = _mock_core_with_ca_secret()
    mock_core.create_namespaced_config_map.side_effect = client.ApiException(status=409)

    _ensure_service_ca_configmap(mock_core, "test-ns")

    mock_core.replace_namespaced_config_map.assert_called_once()


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


@patch("app.services.server_cert._ensure_service_ca_configmap")
@patch("app.services.server_cert._get_route_hostname", return_value="route.example.com")
def test_ensure_server_cert_generates_when_missing(
    _mock_route, _mock_configmap, tmp_path, monkeypatch
):
    """Verify ensure_server_cert generates a cert when none exists on disk."""
    monkeypatch.setattr("app.services.server_cert.TLS_DIR", str(tmp_path))

    ca_key, ca_cert = generate_ca("test-ca")
    ensure_server_cert(ca_key, ca_cert, "test-ns")

    assert os.path.exists(os.path.join(str(tmp_path), "tls.crt"))
    assert os.path.exists(os.path.join(str(tmp_path), "tls.key"))


@patch("app.services.server_cert._ensure_service_ca_configmap")
@patch("app.services.server_cert._get_route_hostname", return_value="route.example.com")
def test_ensure_server_cert_renews_when_expiring(
    _mock_route, _mock_configmap, tmp_path, monkeypatch
):
    """Verify ensure_server_cert renews a cert expiring within the threshold."""
    monkeypatch.setattr("app.services.server_cert.TLS_DIR", str(tmp_path))

    ca_key, ca_cert = generate_ca("test-ca")
    _write_test_cert(tmp_path, ca_key, ca_cert, validity_days=20)

    mock_ctx = MagicMock(spec=ssl.SSLContext)
    set_ssl_context(mock_ctx)
    try:
        old_cert_path = os.path.join(str(tmp_path), "tls.crt")
        with open(old_cert_path, "rb") as f:
            old_cert = x509.load_pem_x509_certificate(f.read())

        ensure_server_cert(ca_key, ca_cert, "test-ns")

        with open(old_cert_path, "rb") as f:
            new_cert = x509.load_pem_x509_certificate(f.read())
        assert new_cert.serial_number != old_cert.serial_number

        mock_ctx.load_cert_chain.assert_called_once()
    finally:
        set_ssl_context(None)


@patch("app.services.server_cert._ensure_service_ca_configmap")
@patch("app.services.server_cert._get_route_hostname", return_value="route.example.com")
def test_ensure_server_cert_noop_when_valid(
    mock_route, _mock_configmap, tmp_path, monkeypatch
):
    """Verify ensure_server_cert is a no-op when cert has plenty of validity left."""
    monkeypatch.setattr("app.services.server_cert.TLS_DIR", str(tmp_path))

    ca_key, ca_cert = generate_ca("test-ca")
    _write_test_cert(tmp_path, ca_key, ca_cert, validity_days=300)

    cert_path = os.path.join(str(tmp_path), "tls.crt")
    with open(cert_path, "rb") as f:
        original_cert = x509.load_pem_x509_certificate(f.read())

    ensure_server_cert(ca_key, ca_cert, "test-ns")

    with open(cert_path, "rb") as f:
        after_cert = x509.load_pem_x509_certificate(f.read())
    assert after_cert.serial_number == original_cert.serial_number

    mock_route.assert_not_called()

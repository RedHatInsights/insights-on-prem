"""Tests for app.server_cert."""

import os
from unittest.mock import MagicMock

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import NameOID
from kubernetes import client

from app.server_cert import (
    _ensure_service_ca_configmap,
    _generate_server_cert,
    _write_cert_files,
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
    monkeypatch.setattr("app.server_cert.TLS_DIR", str(tmp_path))

    ca_key, ca_cert = generate_ca("test-ca")
    _, server_cert = _generate_server_cert(ca_key, ca_cert, ["test.local"])

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


def test_ensure_service_ca_configmap_creates():
    """Verify _ensure_service_ca_configmap creates a ConfigMap when none exists."""
    _, ca_cert = generate_ca("test-ca")
    mock_core = MagicMock(spec=client.CoreV1Api)

    _ensure_service_ca_configmap(mock_core, "test-ns", ca_cert)

    mock_core.create_namespaced_config_map.assert_called_once()
    call_args = mock_core.create_namespaced_config_map.call_args
    cm = call_args[0][1]
    assert "ca-bundle.crt" in cm.data
    assert "BEGIN CERTIFICATE" in cm.data["ca-bundle.crt"]


def test_ensure_service_ca_configmap_updates_on_conflict():
    """Verify _ensure_service_ca_configmap falls back to replace on 409 conflict."""
    _, ca_cert = generate_ca("test-ca")
    mock_core = MagicMock(spec=client.CoreV1Api)
    mock_core.create_namespaced_config_map.side_effect = client.ApiException(status=409)

    _ensure_service_ca_configmap(mock_core, "test-ns", ca_cert)

    mock_core.replace_namespaced_config_map.assert_called_once()

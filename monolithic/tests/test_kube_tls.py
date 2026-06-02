"""Tests for app.utils.kube_tls."""

import base64
import datetime
from unittest.mock import MagicMock

import pytest
from app.utils.kube_tls import (
    build_leaf_cert,
    ensure_ca_secret,
    generate_ca,
    remove_expired_cas,
    renew_ca_secret,
)
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from kubernetes import client


@pytest.fixture
def ca_keypair():
    """Generate a CA keypair for use in leaf cert tests."""
    return generate_ca("test-ca")


def test_generate_ca_returns_valid_ca():
    """Verify generate_ca produces a valid self-signed CA certificate."""
    key, cert = generate_ca("my-test-ca")

    assert (
        cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        == "my-test-ca"
    )
    assert cert.issuer == cert.subject

    bc = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert bc.ca is True
    assert bc.path_length == 0

    ku = cert.extensions.get_extension_for_class(x509.KeyUsage).value
    assert ku.key_cert_sign is True
    assert ku.crl_sign is True
    assert ku.digital_signature is False

    now = datetime.datetime.now(datetime.timezone.utc)
    assert cert.not_valid_before_utc <= now
    assert cert.not_valid_after_utc > now + datetime.timedelta(days=3649)


@pytest.mark.parametrize(
    "eku_oid, sans, expected_san_count",
    [
        (x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH, None, 0),
        (
            x509.oid.ExtendedKeyUsageOID.SERVER_AUTH,
            [x509.DNSName("example.com"), x509.DNSName("*.example.com")],
            2,
        ),
    ],
    ids=["client_auth_no_sans", "server_auth_with_sans"],
)
def test_build_leaf_cert(ca_keypair, eku_oid, sans, expected_san_count):
    """Verify build_leaf_cert produces a correctly configured leaf certificate."""
    ca_key, ca_cert = ca_keypair
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "leaf")])

    cert = build_leaf_cert(
        subject=subject,
        public_key=leaf_key.public_key(),
        ca_key=ca_key,
        ca_cert=ca_cert,
        validity_days=365,
        extended_key_usage=eku_oid,
        sans=sans,
    )

    bc = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert bc.ca is False

    ku = cert.extensions.get_extension_for_class(x509.KeyUsage).value
    assert ku.digital_signature is True
    assert ku.key_encipherment is True
    assert ku.key_cert_sign is False

    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert eku_oid in eku

    if expected_san_count:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        assert len(san.get_values_for_type(x509.DNSName)) == expected_san_count
    else:
        with pytest.raises(x509.ExtensionNotFound):
            cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)


def test_build_leaf_cert_clamps_validity_to_ca():
    """Verify leaf cert validity is clamped when CA expires sooner than requested."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "short-ca")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )

    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = build_leaf_cert(
        subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "leaf")]),
        public_key=leaf_key.public_key(),
        ca_key=key,
        ca_cert=ca_cert,
        validity_days=365,
        extended_key_usage=x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH,
    )

    leaf_validity = leaf.not_valid_after_utc - leaf.not_valid_before_utc
    assert leaf_validity <= datetime.timedelta(days=30)


def _make_ca_secret_data():
    """Helper: generate a CA and return it as base64-encoded secret data."""
    ca_key, ca_cert = generate_ca("test-ca")
    key_pem = ca_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_pem = ca_cert.public_bytes(serialization.Encoding.PEM)
    return {
        "tls.key": base64.b64encode(key_pem).decode(),
        "tls.crt": base64.b64encode(cert_pem).decode(),
    }


def test_ensure_ca_secret_loads_existing():
    """Verify ensure_ca_secret loads an existing secret without creating a new one."""
    mock_core = MagicMock(spec=client.CoreV1Api)
    secret_data = _make_ca_secret_data()
    mock_core.read_namespaced_secret.return_value = MagicMock(data=secret_data)

    ca_key, ca_cert = ensure_ca_secret(mock_core, "test-ns", "my-ca", "test-ca")

    assert (
        ca_cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        == "test-ca"
    )
    mock_core.create_namespaced_secret.assert_not_called()


def test_ensure_ca_secret_creates_new():
    """Verify ensure_ca_secret creates a new secret when none exists."""
    mock_core = MagicMock(spec=client.CoreV1Api)
    mock_core.read_namespaced_secret.side_effect = client.ApiException(status=404)

    ca_key, ca_cert = ensure_ca_secret(mock_core, "test-ns", "my-ca", "new-ca")

    assert (
        ca_cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "new-ca"
    )
    mock_core.create_namespaced_secret.assert_called_once()


def test_ensure_ca_secret_handles_conflict():
    """Verify ensure_ca_secret retries on 409 conflict and loads the winner's CA."""
    secret_data = _make_ca_secret_data()

    mock_core = MagicMock(spec=client.CoreV1Api)
    mock_core.read_namespaced_secret.side_effect = [
        client.ApiException(status=404),
        MagicMock(data=secret_data),
    ]
    mock_core.create_namespaced_secret.side_effect = client.ApiException(status=409)

    ca_key, ca_cert = ensure_ca_secret(mock_core, "test-ns", "my-ca", "test-ca")

    assert (
        ca_cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        == "test-ca"
    )
    assert mock_core.read_namespaced_secret.call_count == 2


def _make_ca_secret_data_with_validity(common_name, validity_days):
    """Helper: generate a CA with specific validity and return as secret data."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=validity_days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    return {
        "tls.key": base64.b64encode(key_pem).decode(),
        "tls.crt": base64.b64encode(cert_pem).decode(),
    }


def test_renew_ca_secret_no_op_when_valid():
    """Verify renew_ca_secret returns existing CA when plenty of validity remains."""
    secret_data = _make_ca_secret_data_with_validity("test-ca", validity_days=3650)
    mock_core = MagicMock(spec=client.CoreV1Api)
    mock_core.read_namespaced_secret.return_value = MagicMock(data=secret_data)

    ca_key, ca_cert = renew_ca_secret(mock_core, "test-ns", "my-ca", "test-ca")

    assert (
        ca_cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        == "test-ca"
    )
    mock_core.replace_namespaced_secret.assert_not_called()


def test_renew_ca_secret_renews_when_expiring():
    """Verify renew_ca_secret generates a new CA and chains it when expiring."""
    secret_data = _make_ca_secret_data_with_validity("old-ca", validity_days=300)
    old_cert_pem = base64.b64decode(secret_data["tls.crt"])
    mock_core = MagicMock(spec=client.CoreV1Api)
    mock_core.read_namespaced_secret.return_value = MagicMock(data=dict(secret_data))

    ca_key, ca_cert = renew_ca_secret(mock_core, "test-ns", "my-ca", "new-ca")

    assert (
        ca_cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "new-ca"
    )
    mock_core.replace_namespaced_secret.assert_called_once()

    updated_secret = mock_core.replace_namespaced_secret.call_args[0][2]
    chain_pem = base64.b64decode(updated_secret.data["tls.crt"])
    assert chain_pem.count(b"BEGIN CERTIFICATE") == 2
    assert old_cert_pem in chain_pem


def test_remove_expired_cas():
    """Verify remove_expired_cas strips expired certs from the chain."""
    _, valid_cert = generate_ca("valid-ca")
    valid_pem = valid_cert.public_bytes(serialization.Encoding.PEM)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)
    expired_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "expired")]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "expired")]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=10))
        .not_valid_after(now - datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    expired_pem = expired_cert.public_bytes(serialization.Encoding.PEM)

    chain = base64.b64encode(valid_pem + expired_pem).decode()
    secret_data = {"tls.crt": chain, "tls.key": "unused"}
    mock_core = MagicMock(spec=client.CoreV1Api)
    mock_core.read_namespaced_secret.return_value = MagicMock(data=secret_data)

    remove_expired_cas(mock_core, "test-ns", "my-ca")

    mock_core.replace_namespaced_secret.assert_called_once()
    updated = mock_core.replace_namespaced_secret.call_args[0][2]
    pruned_pem = base64.b64decode(updated.data["tls.crt"])
    assert pruned_pem.count(b"BEGIN CERTIFICATE") == 1
    assert b"expired" not in pruned_pem

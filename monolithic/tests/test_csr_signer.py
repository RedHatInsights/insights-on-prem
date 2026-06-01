"""Tests for app.services.csr_signer."""

import base64
from unittest.mock import MagicMock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.services.csr_signer import _process_csr, _should_process, _sign_csr
from app.utils.kube_tls import generate_ca


def _make_csr_pem() -> bytes:
    """Generate a CSR PEM for testing."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-spoke")])
        )
        .sign(key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM)


def _make_event(
    event_type="ADDED",
    username="system:open-cluster-management:test",
    certificate=None,
    conditions=None,
):
    """Build a minimal watch event dict for _should_process tests."""
    status = MagicMock()
    status.certificate = certificate
    status.conditions = conditions

    obj = MagicMock()
    obj.metadata.name = "test-csr"
    obj.spec.username = username
    obj.status = status

    return {"type": event_type, "object": obj}


def test_sign_csr_returns_valid_cert():
    """Verify _sign_csr produces a valid client certificate signed by the CA."""
    ca_key, ca_cert = generate_ca("test-ca")
    csr_pem = _make_csr_pem()

    signed_pem = _sign_csr(csr_pem, ca_key, ca_cert)
    cert = x509.load_pem_x509_certificate(signed_pem)

    assert (
        cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        == "test-spoke"
    )
    assert cert.issuer == ca_cert.subject

    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH in eku


def test_should_process_added_event():
    """Verify _should_process returns username for a valid ADDED event."""
    event = _make_event()
    assert _should_process(event) == "system:open-cluster-management:test"


def test_should_process_skips_non_added():
    """Verify _should_process skips DELETED events."""
    event = _make_event(event_type="DELETED")
    assert _should_process(event) is None


def test_should_process_skips_already_signed():
    """Verify _should_process skips CSRs that already have a certificate."""
    event = _make_event(certificate=b"some-cert-data")
    assert _should_process(event) is None


def test_should_process_skips_already_approved():
    """Verify _should_process skips CSRs that are already approved."""
    condition = MagicMock()
    condition.type = "Approved"
    event = _make_event(conditions=[condition])
    assert _should_process(event) is None


def test_should_process_rejects_bad_username():
    """Verify _should_process rejects CSRs from unauthorized users."""
    event = _make_event(username="system:serviceaccount:default:hacker")
    assert _should_process(event) is None


def test_process_csr_approves_and_signs():
    """Verify _process_csr calls approve and uploads the signed certificate."""
    ca_key, ca_cert = generate_ca("test-ca")
    csr_pem = _make_csr_pem()

    csr_obj = MagicMock()
    csr_obj.metadata.name = "test-csr"
    csr_obj.spec.request = base64.b64encode(csr_pem).decode()

    mock_certs_v1 = MagicMock()

    _process_csr(mock_certs_v1, csr_obj, ca_key, ca_cert)

    mock_certs_v1.patch_certificate_signing_request_approval.assert_called_once()
    mock_certs_v1.patch_certificate_signing_request_status.assert_called_once()

    call_args = mock_certs_v1.patch_certificate_signing_request_status.call_args
    assert call_args[0][0] == "test-csr"
    cert_b64 = call_args[1]["body"]["status"]["certificate"]
    cert = x509.load_pem_x509_certificate(base64.b64decode(cert_b64))
    assert (
        cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        == "test-spoke"
    )

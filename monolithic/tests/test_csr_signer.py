"""Tests for app.services.csr_signer."""

import base64
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from app.services.csr_signer import CSRSigner
from app.utils.kube_tls import generate_ca
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


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


def _make_signer():
    """Create a CSRSigner with a stub TLSManager holding test CA keys."""
    ca_key, ca_cert = generate_ca("test-ca")
    tls_manager = SimpleNamespace(client_ca_key=ca_key, client_ca_cert=ca_cert)
    return CSRSigner(tls_manager), ca_cert


def _make_event(
    event_type="ADDED",
    username="system:open-cluster-management:test",
    certificate=None,
    conditions=None,
    csr_name="test-csr",
    csr_pem=None,
):
    """Build a minimal watch event dict."""
    status = MagicMock()
    status.certificate = certificate
    status.conditions = conditions

    obj = MagicMock()
    obj.metadata.name = csr_name
    obj.spec.username = username
    obj.status = status
    if csr_pem is not None:
        obj.spec.request = base64.b64encode(csr_pem).decode()

    return {"type": event_type, "object": obj}


async def _run_with_events(signer, events):
    """Run signer.run() with a mock watch that yields the given events once.

    Uses SystemExit to break the infinite watch loop after the first iteration.
    """
    mock_certs_v1 = MagicMock()

    first_watch = MagicMock()
    first_watch.stream.return_value = events
    watches = [first_watch]

    def watch_factory():
        if watches:
            return watches.pop(0)
        raise SystemExit

    with (
        patch(
            "app.services.csr_signer.client.CertificatesV1Api",
            return_value=mock_certs_v1,
        ),
        patch("app.services.csr_signer.watch.Watch", side_effect=watch_factory),
        pytest.raises(SystemExit),
    ):
        await signer.run()

    return mock_certs_v1


@pytest.mark.asyncio
async def test_run_approves_and_signs_valid_csr():
    """Verify run() approves a valid CSR and uploads the signed certificate."""
    signer, ca_cert = _make_signer()
    csr_pem = _make_csr_pem()

    event = _make_event(csr_pem=csr_pem)
    mock_certs_v1 = await _run_with_events(signer, [event])

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
    assert cert.issuer == ca_cert.subject

    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH in eku


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_kwargs",
    [
        pytest.param({"event_type": "DELETED"}, id="deleted_event"),
        pytest.param({"certificate": b"some-cert-data"}, id="already_signed"),
        pytest.param({"conditions": [MagicMock(type="Approved")]}, id="already_approved"),
        pytest.param(
            {"username": "system:serviceaccount:default:hacker"}, id="unauthorized_user"
        ),
    ],
)
async def test_run_skips_invalid_event(event_kwargs):
    """Verify run() skips events that should not be processed."""
    signer, _ = _make_signer()
    event = _make_event(**event_kwargs)
    mock_certs_v1 = await _run_with_events(signer, [event])

    mock_certs_v1.patch_certificate_signing_request_approval.assert_not_called()



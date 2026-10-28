"""Build a public-CA-only Java truststore inside a layer, before Trino starts.

The namespace-local projected Secret is never read through the Kubernetes API.
Updates require a controlled rollout; use a new Secret name for each CA version.
"""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import uuid

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs12


PASSWORD = b'changeit'  # Integrity password for PUBLIC certificates, not a credential.
MAX_BYTES = 1024 * 1024


def trusted_certificates(payload):
    if not payload or len(payload) > MAX_BYTES:
        raise ValueError('CA bundle size invalid')
    # Reject private keys and other PEM blocks rather than silently discarding them.
    blocks = re.findall(rb'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', payload, re.S)
    remaining = payload
    for block in blocks:
        remaining = remaining.replace(block, b'', 1)
    if not blocks or remaining.strip():
        raise ValueError('CA bundle must contain only PEM certificates')
    now = datetime.now(timezone.utc)
    unique = {}
    for block in blocks:
        cert = x509.load_pem_x509_certificate(block)
        if not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
            raise ValueError('CA certificate is not currently valid')
        try:
            is_ca = cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        except x509.ExtensionNotFound:
            is_ca = False
        if not is_ca:
            raise ValueError('Trust anchors must be CA certificates')
        try:
            if not cert.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign:
                raise ValueError('CA does not permit certificate signing')
        except x509.ExtensionNotFound:
            pass  # Older public roots do not always carry KeyUsage.
        unique[cert.fingerprint(hashes.SHA256())] = cert
    return [pkcs12.PKCS12Certificate(unique[key], key.hex().encode()) for key in sorted(unique)]


def build(source, output):
    source, output = Path(source), Path(output)
    # Resolve a single kubelet projection revision before reading the selected key.
    projection = source.parent / '..data'
    if projection.is_symlink():
        source = projection.resolve(strict=True) / source.name
    with source.open('rb') as stream:
        certs = trusted_certificates(stream.read(MAX_BYTES + 1))
    payload = pkcs12.serialize_java_truststore(certs, serialization.BestAvailableEncryption(PASSWORD))
    temporary = output.with_name('.trust-' + uuid.uuid4().hex)
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        build(args.input, args.output)
    except (OSError, ValueError, TypeError, UnsupportedAlgorithm, x509.ExtensionNotFound, x509.DuplicateExtension):
        raise SystemExit('Java CA bundle unavailable or invalid; verify public CA certificates and their validity') from None


if __name__ == '__main__':
    main()

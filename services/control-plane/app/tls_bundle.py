"""Validate and atomically publish a local TLS Secret as Trino's combined PEM.

Runs as a token-less init/sidecar in a layer. Never reads Kubernetes API Secrets,
copies keys across namespaces, or logs certificate/key payloads.
"""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import time
import uuid

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization


def validated_bundle(directory, hostname):
    directory = Path(directory)
    # Pin Kubernetes' atomic Secret projection so key and certificate come from
    # the same revision, even if kubelet replaces ..data between file reads.
    projection = directory / '..data'
    if projection.is_symlink():
        directory = projection.resolve(strict=True)
    key_bytes = (directory / 'tls.key').read_bytes()
    cert_bytes = (directory / 'tls.crt').read_bytes()
    if len(key_bytes) > 65536 or len(cert_bytes) > 1024 * 1024:
        raise ValueError('TLS input exceeds size limit')
    key = serialization.load_pem_private_key(key_bytes, password=None)
    chain = x509.load_pem_x509_certificates(cert_bytes)
    if not chain:
        raise ValueError('TLS certificate chain is empty')
    cert = chain[0]
    public = lambda obj: obj.public_key().public_bytes(serialization.Encoding.DER,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo)
    if public(cert) != public(key):
        raise ValueError('TLS key does not match certificate')
    now = datetime.now(timezone.utc)
    if not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
        raise ValueError('TLS certificate is not currently valid')
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    # This internal endpoint has a stable, exact namespace-qualified DNS name.
    # Explicit names avoid accepting unexpectedly broad wildcard identities.
    if hostname not in names:
        raise ValueError('TLS certificate does not cover the layer endpoint')
    return key_bytes.rstrip() + b'\n' + cert_bytes.rstrip() + b'\n'


def publish(directory, output, hostname):
    output = Path(output)
    ready = output.parent / 'ready'
    try:
        payload = validated_bundle(directory, hostname)
        if not output.exists() or output.read_bytes() != payload:
            temporary = output.with_name('.pem-' + uuid.uuid4().hex)
            try:
                with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, output)
            finally:
                temporary.unlink(missing_ok=True)
        ready.touch(mode=0o600)
        return True
    except (OSError, ValueError, TypeError, UnsupportedAlgorithm, x509.ExtensionNotFound, x509.DuplicateExtension):
        ready.unlink(missing_ok=True)
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--hostname', required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    while True:
        valid = publish(args.input, args.output, args.hostname)
        if args.once:
            if not valid:
                raise SystemExit('TLS certificate unavailable or invalid; verify key pair, validity and layer DNS names')
            return
        if not valid:
            print('TLS certificate unavailable or invalid; readiness withheld until corrected', flush=True)
        time.sleep(5)


if __name__ == '__main__':
    main()

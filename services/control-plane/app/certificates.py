"""Certificate secrets: validate what was uploaded and describe it without revealing key material."""
from __future__ import annotations

import base64
from datetime import timezone
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException

MAX_PEM = 256 * 1024


def _fail(field: str, message: str):
    raise HTTPException(422, {"message": "certificate is not valid", "errors": {field: message}})


def _load_certs(pem: str, field: str) -> list:
    from cryptography import x509
    try:
        certs = x509.load_pem_x509_certificates(pem.encode())
    except ValueError:
        _fail(field, "paste PEM certificates (-----BEGIN CERTIFICATE-----)")
    if not certs:
        _fail(field, "no certificate found")
    return certs


def _pem(cert) -> str:
    from cryptography.hazmat.primitives.serialization import Encoding
    return cert.public_bytes(Encoding.PEM).decode()


def _key_pem(key) -> str:
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
    return key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()


def _same_key(cert, key) -> bool:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    fmt = (Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return cert.public_key().public_bytes(*fmt) == key.public_key().public_bytes(*fmt)


def bundle(certificate: Optional[str], private_key: Optional[str], ca_chain: Optional[str],
           pkcs12_base64: Optional[str], password: Optional[str]) -> Dict[str, str]:
    """Normalized PEM bundle to store: certificate (leaf), optional private_key (PKCS#8) and ca_chain."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key, pkcs12
    if any(len(v or "") > MAX_PEM for v in (certificate, private_key, ca_chain, pkcs12_base64)):
        _fail("certificate", "certificate material is limited to 256 KB")
    pw = password.encode() if password else None
    if pkcs12_base64:
        try:
            key, leaf, extra = pkcs12.load_key_and_certificates(base64.b64decode(pkcs12_base64), pw)
        except (ValueError, TypeError):
            _fail("pkcs12", "cannot open this PKCS#12 file; check the password")
        if leaf is None:
            _fail("pkcs12", "the PKCS#12 file contains no certificate")
        chain = list(extra or [])
    else:
        if not (certificate or "").strip():
            _fail("certificate", "required")
        certs = _load_certs(certificate, "certificate")
        leaf, chain = certs[0], certs[1:]
        key = None
        if (private_key or "").strip():
            try:
                key = load_pem_private_key(private_key.encode(), pw)
            except TypeError:
                _fail("private_key", "this key is encrypted; enter its password")
            except ValueError:
                _fail("private_key", "paste a PEM private key, or check the password")
    if (ca_chain or "").strip():
        chain += _load_certs(ca_chain, "ca_chain")
    if key is not None and not _same_key(leaf, key):
        _fail("private_key", "this private key does not belong to the certificate")
    out = {"certificate": _pem(leaf)}
    if key is not None:
        out["private_key"] = _key_pem(key)
    if chain:
        out["ca_chain"] = "".join(_pem(c) for c in chain)
    return out


def describe(stored: Dict[str, str]) -> Dict[str, object]:
    """Public facts about a stored certificate (never the key)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    leaf = x509.load_pem_x509_certificate(stored["certificate"].encode())
    try:
        names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        san = [str(n.value) for n in names]
    except x509.ExtensionNotFound:
        san = []
    chain = stored.get("ca_chain", "")
    return {
        "subject": leaf.subject.rfc4514_string(),
        "issuer": leaf.issuer.rfc4514_string(),
        "serial": format(leaf.serial_number, "x"),
        "not_before": leaf.not_valid_before_utc.astimezone(timezone.utc).isoformat(),
        "not_after": leaf.not_valid_after_utc.astimezone(timezone.utc).isoformat(),
        "sha256_fingerprint": leaf.fingerprint(hashes.SHA256()).hex(":").upper(),
        "subject_alt_names": san[:50],
        "has_private_key": bool(stored.get("private_key")),
        "chain_length": chain.count("BEGIN CERTIFICATE"),
    }


def env_for(var: str, stored: Dict[str, str]) -> List[Tuple[str, str]]:
    """How a certificate reaches a program: VAR (certificate), VAR_KEY and VAR_CA when present."""
    out = [(var, stored["certificate"])]
    if stored.get("private_key"):
        out.append((var + "_KEY", stored["private_key"]))
    if stored.get("ca_chain"):
        out.append((var + "_CA", stored["ca_chain"]))
    return out

"""Streaming recovery envelopes: customer RSA key wraps a per-archive AES-GCM key.

The private recovery key never enters the control plane. Restore authenticates the
entire envelope before exposing any plaintext to pg_restore. A trusted ciphertext
checksum is required by the offline restore command to establish archive provenance.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b"FDPRECOVERY1\n"
CHUNK = 1024 * 1024


def public_key(pem):
    key = serialization.load_pem_public_key(pem.encode())
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 3072:
        raise ValueError("recovery key must be an RSA public key of at least 3072 bits")
    return key


def fingerprint(key):
    return hashlib.sha256(key.public_bytes(serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()


def checksum(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b''):
            digest.update(chunk)
    return digest.hexdigest()


def encrypt(source, destination, pem):
    recipient = public_key(pem)
    key, nonce = os.urandom(32), os.urandom(12)
    header = json.dumps({'version':1, 'algorithm':'RSA-OAEP-SHA256+AES-256-GCM',
        'key_id':fingerprint(recipient), 'nonce':base64.b64encode(nonce).decode(),
        'wrapped_key':base64.b64encode(recipient.encrypt(key, padding.OAEP(
            mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=MAGIC))).decode()},
        sort_keys=True, separators=(',', ':')).encode()
    aad = MAGIC + struct.pack('!I', len(header)) + header
    cipher = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    cipher.authenticate_additional_data(aad)
    with Path(source).open('rb') as plain, Path(destination).open('xb') as output:
        os.chmod(destination, 0o600)
        output.write(aad)
        for chunk in iter(lambda: plain.read(CHUNK), b''):
            output.write(cipher.update(chunk))
        output.write(cipher.finalize())
        output.write(cipher.tag)


def decrypt(source, destination, private_pem, expected_sha256, password=None):
    if Path(destination).exists():
        raise FileExistsError('refusing to overwrite an existing recovery output')
    if len(expected_sha256) != 64 or checksum(source) != expected_sha256.lower():
        raise ValueError('archive does not match the trusted SHA-256 checksum')
    key = serialization.load_pem_private_key(private_pem, password=password)
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 3072:
        raise ValueError('invalid recovery private key')
    try:
        with Path(source).open('rb') as encrypted:
            if encrypted.read(len(MAGIC)) != MAGIC:
                raise ValueError('unsupported recovery envelope')
            size_bytes = encrypted.read(4)
            if len(size_bytes) != 4:
                raise ValueError('truncated recovery envelope')
            size = struct.unpack('!I', size_bytes)[0]
            if not 0 < size <= 16384:
                raise ValueError('invalid recovery header')
            raw = encrypted.read(size)
            header = json.loads(raw)
            if header.get('version') != 1 or header.get('key_id') != fingerprint(key.public_key()):
                raise ValueError('recovery key or envelope version does not match')
            wrapped = base64.b64decode(header['wrapped_key'], validate=True)
            aes = key.decrypt(wrapped, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                algorithm=hashes.SHA256(), label=MAGIC))
            nonce = base64.b64decode(header['nonce'], validate=True)
            start = encrypted.tell()
            encrypted.seek(-16, 2)
            length, tag = encrypted.tell() - start, encrypted.read(16)
            if length < 0 or len(nonce) != 12 or len(aes) != 32:
                raise ValueError('invalid recovery envelope')
            encrypted.seek(start)
            cipher = Cipher(algorithms.AES(aes), modes.GCM(nonce, tag)).decryptor()
            cipher.authenticate_additional_data(MAGIC + size_bytes + raw)
            with Path(destination).open('xb') as plain:
                os.chmod(destination, 0o600)
                while length:
                    chunk = encrypted.read(min(length, CHUNK))
                    if not chunk:
                        raise ValueError('truncated recovery envelope')
                    length -= len(chunk)
                    plain.write(cipher.update(chunk))
                plain.write(cipher.finalize())
    except Exception:
        Path(destination).unlink(missing_ok=True)
        raise

"""Signing keys rotate with an overlap: former keys verify (never sign) until they are removed."""
import time

from fastapi import HTTPException
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app import sql_access
from app.auth import mint_token, read_token
from app.config import get_settings

OLD, NEW = 'old-session-key-' + 'a' * 32, 'new-session-key-' + 'b' * 32


def test_session_tokens_survive_a_rotation_only_while_the_former_key_is_listed(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, 'session_signing_key', OLD)
    issued_before = mint_token('execution', 'ana', 3600, run='r1')
    monkeypatch.setattr(settings, 'session_signing_key', NEW)
    monkeypatch.setattr(settings, 'session_signing_previous_keys', OLD)
    assert read_token(issued_before, 'execution')['sub'] == 'ana'          # overlap: still accepted
    issued_after = mint_token('execution', 'bob', 3600)
    assert jwt.decode(issued_after, NEW, algorithms=['HS256'])['sub'] == 'bob'  # new tokens use the new key
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(issued_after, OLD, algorithms=['HS256'])
    monkeypatch.setattr(settings, 'session_signing_previous_keys', '')
    with pytest.raises(HTTPException) as error:                            # former key removed: rejected
        read_token(issued_before, 'execution')
    assert error.value.status_code == 401


def test_rotation_never_extends_an_expired_token(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, 'session_signing_key', OLD)
    expired = jwt.encode({'typ': 'execution', 'sub': 'ana', 'exp': int(time.time()) - 10}, OLD, algorithm='HS256')
    monkeypatch.setattr(settings, 'session_signing_key', NEW)
    monkeypatch.setattr(settings, 'session_signing_previous_keys', OLD)
    with pytest.raises(HTTPException) as error:
        read_token(expired, 'execution')
    assert 'expired' in str(error.value.detail).lower()


def pem():
    return ec.generate_private_key(ec.SECP256R1()).private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()).decode()


def test_layer_jwks_keeps_former_keys_during_a_rotation(monkeypatch):
    settings = get_settings()
    old, new = pem(), pem()
    monkeypatch.setattr(settings, 'trino_jwt_private_key', old)
    in_flight = sql_access.layer_engine_token('p1', '1a0000000002')
    monkeypatch.setattr(settings, 'trino_jwt_private_key', new)
    monkeypatch.setattr(settings, 'trino_jwt_previous_keys', old)
    keys = {k['kid']: jwt.PyJWK(k).key for k in sql_access.layer_jwks()['keys']}
    assert len(keys) == 2
    kid = jwt.get_unverified_header(in_flight)['kid']
    assert jwt.decode(in_flight, keys[kid], algorithms=['ES256'], audience='trino-layer-1a0000000002')['sub'] == 'p1'
    fresh = sql_access.layer_engine_token('p2', '1a0000000002')
    assert jwt.get_unverified_header(fresh)['kid'] != kid                 # new queries use the new key
    monkeypatch.setattr(settings, 'trino_jwt_previous_keys', '')
    assert len(sql_access.layer_jwks()['keys']) == 1
    assert sql_access.layer_jwks()['keys'][0]['kid'] == jwt.get_unverified_header(fresh)['kid']

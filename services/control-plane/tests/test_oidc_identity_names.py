"""An IdP profile name must never select a managed identity in the RBAC resolver."""
import time
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
import jwt
import pytest

from app.auth import OIDCVerifier
from app.config import get_settings


@pytest.mark.parametrize('name',['service:abcdef123456','sa:finance-etl'])
def test_valid_idp_signature_does_not_allow_reserved_service_username(name,monkeypatch):
    settings=get_settings()
    key=ec.generate_private_key(ec.SECP256R1())
    verifier=OIDCVerifier(settings)
    monkeypatch.setattr(verifier,'_jwks_client',lambda:SimpleNamespace(
        get_signing_key_from_jwt=lambda token:SimpleNamespace(key=key.public_key())))
    claims={'sub':'real-human-subject','iss':settings.oidc_issuer,'aud':settings.oidc_audience,
            'exp':int(time.time())+60,settings.oidc_username_claim:name}
    with pytest.raises(HTTPException,match='reserved managed-identity prefix') as error:
        verifier.verify(jwt.encode(claims,key,algorithm='ES256'))
    assert error.value.status_code==401
    claims[settings.oidc_username_claim]='ana'
    human=verifier.verify(jwt.encode(claims,key,algorithm='ES256'))
    assert human.username=='ana' and human.subject=='real-human-subject'

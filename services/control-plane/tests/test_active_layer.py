"""The app's layer mode: a per-user choice of layer that falls back to the default layer."""
from app import layers
from app.db import SessionLocal
from app.models import UserPreference
from .conftest import as_


def listed(client, user='ana'):
    return client.get('/v1/layers', headers=as_(user)).json()


def test_layer_mode_defaults_is_chosen_per_user_and_survives_requests(client):
    available = listed(client)
    default = next((l['id'] for l in available if l['default']), None)
    assert client.get('/v1/me', headers=as_('ana')).json()['active_layer_id'] == default
    other = next(l['id'] for l in available if l['id'] != default)
    response = client.put('/v1/me/active-layer', headers=as_('ana'), json={'layer_id': other})
    assert response.status_code == 200, response.text
    assert client.get('/v1/me', headers=as_('ana')).json()['active_layer_id'] == other
    # Another user's mode is unaffected.
    assert client.get('/v1/me', headers=as_('bob')).json()['active_layer_id'] == default


def test_unknown_layers_are_refused_and_a_removed_layer_falls_back_to_the_default(client):
    assert client.put('/v1/me/active-layer', headers=as_('ana'), json={'layer_id': 'ffffffffffff'}).status_code == 404
    assert client.put('/v1/me/active-layer', headers=as_('ana'), json={'layer_id': 'x', 'extra': 1}).status_code == 422
    principal = client.get('/v1/me', headers=as_('ana')).json()['principal_id']
    with SessionLocal() as db:
        preference = db.get(UserPreference, principal) or UserPreference(principal_id=principal)
        preference.active_layer_id = 'gone00000000'
        db.merge(preference)
        db.commit()
        default = layers.default_layer(db)
    assert client.get('/v1/me', headers=as_('ana')).json()['active_layer_id'] == default

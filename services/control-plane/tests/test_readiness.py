from sqlalchemy.exc import OperationalError

from app import main


def test_readiness_reports_initialization_and_metadata_recovery_without_liveness_failure(client, monkeypatch):
    assert client.get('/readyz').status_code == 200
    monkeypatch.setattr(main.app.state, 'initialized', False)
    assert client.get('/readyz').json() == {'ok': False, 'reason': 'Initializing'}
    assert client.get('/readyz').status_code == 503
    monkeypatch.setattr(main.app.state, 'initialized', True)
    original = main.engine.connect

    def unavailable():
        raise OperationalError('postgres://private-credentials', {}, Exception('private-database-host'))

    monkeypatch.setattr(main.engine, 'connect', unavailable)
    response = client.get('/readyz')
    assert response.status_code == 503
    assert response.json() == {'ok': False, 'reason': 'MetadataUnavailable'}
    assert 'private' not in response.text
    assert client.get('/healthz').status_code == 200
    monkeypatch.setattr(main.engine, 'connect', original)
    assert client.get('/readyz').status_code == 200

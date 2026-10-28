from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.routes import gateway


@pytest.mark.parametrize('allowed', [True, False])
def test_asset_authorization_releases_connections_before_dependency_teardown(tmp_path, monkeypatch, allowed):
    engine = create_engine(f'sqlite:///{tmp_path}/auth.db',pool_size=2,max_overflow=0,pool_timeout=2,
                           connect_args={'check_same_thread':False})
    sessions = []
    monkeypatch.setattr(gateway,'read_token',lambda *args: {'target':'apps:example','sub':'ana'})
    monkeypatch.setattr(gateway,'_touch',lambda *args: None)

    def check(db, *args):
        db.execute(text('SELECT 1'))
        return allowed

    monkeypatch.setattr(gateway,'_allowed',check)

    def asset_request(_):
        db = Session(engine)
        sessions.append(db)
        request = Request({'type':'http','headers':[(b'cookie',b'dc_apps_example=session')]})
        try:
            return gateway.forward_auth(request,db,'/apps/example/static/js/DataFrame.js',None,None).status_code
        except HTTPException as error:
            return error.status_code
        # Simulate dependency cleanup waiting behind the incoming asset burst:
        # sessions are intentionally not closed by the caller until all return.

    try:
        with ThreadPoolExecutor(max_workers=32) as workers:
            statuses = list(workers.map(asset_request,range(64)))
        assert statuses == [200 if allowed else 403] * 64
        assert engine.pool.checkedout() == 0
    finally:
        for db in sessions:
            db.close()
        engine.dispose()


@pytest.mark.parametrize('failures,allowed,expected', [(1, True, 200), (2, False, 403), (3, True, 503)])
def test_asset_authorization_retries_connection_failure(monkeypatch, failures, allowed, expected):
    from unittest.mock import Mock
    from sqlalchemy.exc import OperationalError

    db = Mock()
    calls = []
    monkeypatch.setattr(gateway.time, 'sleep', lambda _: None)
    monkeypatch.setattr(gateway, 'read_token', lambda *args: {'target': 'apps:example', 'sub': 'ana'})
    monkeypatch.setattr(gateway, '_touch', lambda *args: None)

    def check(*args):
        calls.append(True)
        if len(calls) <= failures:
            raise OperationalError('connect', {}, OSError('temporary DNS failure'))
        return allowed

    monkeypatch.setattr(gateway, '_allowed', check)
    request = Request({'type': 'http', 'headers': [(b'cookie', b'dc_apps_example=session')]})
    if expected == 503:
        with pytest.raises(OperationalError):
            gateway.forward_auth(request, db, '/apps/example/static/js/DataFrame.js', None, None)
    elif expected == 403:
        with pytest.raises(HTTPException) as error:
            gateway.forward_auth(request, db, '/apps/example/static/js/DataFrame.js', None, None)
        assert error.value.status_code == 403
    else:
        assert gateway.forward_auth(request, db, '/apps/example/static/js/DataFrame.js', None, None).status_code == 200
    assert len(calls) == min(failures + 1, 3)
    assert db.close.call_count == len(calls)


def test_asset_authorization_does_not_retry_non_connection_sql_errors(monkeypatch):
    from unittest.mock import Mock
    from sqlalchemy.exc import OperationalError

    class InvalidSql(Exception):
        sqlstate = '42601'

    db = Mock()
    check = Mock(side_effect=OperationalError('query', {}, InvalidSql()))
    monkeypatch.setattr(gateway, '_forward_auth', check)
    with pytest.raises(OperationalError):
        gateway.forward_auth(Mock(), db, '/', None, None)
    assert check.call_count == 1
    db.close.assert_called_once()

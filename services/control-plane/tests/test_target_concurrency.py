"""Permission targets remain stable across independent discovery transactions."""
import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import Session

from app.models import Resource, DirectoryPrincipal
from app.object_access import ensure_target
from app.layers import PROD, NON_PROD
from .conftest import as_


def test_concurrent_discovery_keeps_both_transactions(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'targets.db'), connect_args={'timeout': 15})
    Resource.__table__.create(engine)
    with Session(engine) as db:
        db.add(Resource(id='schema', kind='schema', name='raw', object_id='source', config={}))
        db.commit()
    ident = hashlib.sha256(b'schema\0table\0cards').hexdigest()[:12]
    barrier = Barrier(2)

    class ConcurrentSession(Session):
        def get(self, entity, key, *args, **kwargs):
            row = super().get(entity, key, *args, **kwargs)
            if key == ident and not self.info.get('discovered'):
                self.info['discovered'] = True
                assert row is None
                barrier.wait(timeout=10)
            return row

    def discover(index):
        with ConcurrentSession(engine) as db:
            target = ensure_target(db, db.get(Resource, 'schema'), 'table', 'cards')
            db.add(Resource(id='other'+str(index), kind='table', name='other'+str(index),
                            parent_id='schema', object_id='source', config={}))
            db.commit()
            return target.id

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(discover, (1, 2))) == [ident, ident]
        with Session(engine) as db:
            assert db.scalar(select(func.count()).select_from(Resource)) == 4
            row = db.get(Resource, ident)
            row.name = 'unrelated'
            db.commit()
            with pytest.raises(HTTPException) as exc:
                ensure_target(db, db.get(Resource, 'schema'), 'table', 'cards')
            assert exc.value.status_code == 409
    finally:
        engine.dispose()


def test_layer_service_may_receive_only_explicit_global_package_access(client):
    from app.db import SessionLocal
    with SessionLocal() as db:
        principal = DirectoryPrincipal(kind='service', provider='local', external_id='prod-runtime', name='Prod runtime', active=True)
        db.add(principal)
        db.flush()
        pid = principal.id
        db.add_all([
            Resource(id='prod-runtime', kind='service', name='Prod runtime', object_id='prod-runtime', config={'layer_id': PROD}),
            Resource(id='test-data', kind='schema', name='Test data', config={'layer_id': NON_PROD}),
            Resource(id='test-package', kind='package', name='Test package', config={'layer_id': NON_PROD}),
        ])
        db.commit()
    def grant(target):
        return client.put('/v1/rbac/resources/'+target+'/assignments', headers=as_('admin'),
                          json={'principal_id': pid, 'role': 'Executor'})
    assert grant('packages').status_code == 200
    for target in ('test-data', 'test-package'):
        response = grant(target)
        assert response.status_code == 422 and 'same layer' in response.text

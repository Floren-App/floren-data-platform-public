from datetime import timedelta

from sqlalchemy import select

from app.auth import User
from app import rbac
from app.db import SessionLocal
from app.models import ComputePool, Run, utcnow
from .conftest import as_


def test_recent_updates_find_older_jobs_that_finish_now_without_exposing_other_users(client):
    client.get('/v1/me', headers=as_('ana'))
    with SessionLocal() as db:
        ana = rbac.resolve_user(db, User('ana'))
        pool = db.scalar(select(ComputePool).limit(1))
        now = utcnow()
        for i in range(10):
            db.add(Run(owner='ana', principal_id=ana.principal_id, pool_id=pool.id, kind='script',
                entrypoint=f'new-{i}.py', status='succeeded', created_at=now-timedelta(hours=i+1),
                finished_at=now-timedelta(hours=i+1)+timedelta(minutes=5)))
        db.add(Run(owner='ana', principal_id=ana.principal_id, pool_id=pool.id, kind='script',
            entrypoint='long-job.py', status='succeeded', created_at=now-timedelta(days=5), finished_at=now))
        db.add(Run(owner='bob', pool_id=pool.id, kind='script', entrypoint='private.py', status='succeeded',
            created_at=now, finished_at=now+timedelta(minutes=1)))
        db.commit()
    created = client.get('/v1/runs?limit=4', headers=as_('ana')).json()
    updated = client.get('/v1/runs?limit=4&sort=updated', headers=as_('ana')).json()
    assert len(created) == len(updated) == 4
    assert not any(r['entrypoint']=='long-job.py' for r in created)
    assert updated[0]['entrypoint'] == 'long-job.py'
    assert not any(r['entrypoint']=='private.py' for r in created+updated)
    assert client.get('/v1/runs?sort=unknown', headers=as_('ana')).status_code == 422

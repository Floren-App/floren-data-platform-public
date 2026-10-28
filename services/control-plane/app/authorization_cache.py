"""Authorization reuse inside one transaction, never across requests or mutations."""
from dataclasses import fields
from functools import wraps

from sqlalchemy import event
from sqlalchemy.orm import Session

from .auth import User


def invalidate(db, *args):
    db.info.pop('authorization_cache', None)
    # These existing bulk snapshots obey the same transaction/mutation boundary.
    for key in list(db.info):
        if isinstance(key, tuple) and key[0] in ('browsing_scope', 'catalog_permissions'):
            db.info.pop(key, None)


@event.listens_for(Session, 'before_flush')
def before_flush(db, *args):
    invalidate(db)


@event.listens_for(Session, 'after_transaction_end')
def transaction_end(db, transaction):
    invalidate(db)


@event.listens_for(Session, 'do_orm_execute')
def execute(state):
    if state.is_update or state.is_delete or state.is_insert:
        invalidate(state.session)


def key(value):
    if isinstance(value, User):
        return tuple((f.name, key(getattr(value, f.name))) for f in fields(value))
    if isinstance(value, (list, tuple)):
        return tuple(key(v) for v in value)
    if isinstance(value, dict):
        return frozenset((key(k), key(v)) for k, v in value.items())
    if isinstance(value, (set, frozenset)):
        return frozenset(key(v) for v in value)
    if hasattr(value, '__mapper__'):
        return (type(value), value.id)
    return value


def cached(function):
    @wraps(function)
    def call(db, *args, **kwargs):
        # Explicit directory refreshes must always reconcile current claims.
        if kwargs.get('refresh') or (function.__name__ == 'resolve_user' and len(args) > 1 and args[1]):
            invalidate(db)
            return function(db, *args, **kwargs)
        # Queries ordinarily autoflush. Do it before reading a snapshot too, so a
        # pending revocation/layer move cannot reuse pre-mutation authorization.
        if db.autoflush and (db.new or db.dirty or db.deleted):
            db.flush()
        cache_key = (function.__module__, function.__name__, key(args), tuple(sorted((k, key(v)) for k, v in kwargs.items())))
        entries = db.info.setdefault('authorization_cache', {})
        if cache_key in entries:
            return entries[cache_key]
        result = function(db, *args, **kwargs)
        # A loader can flush/bind a legacy identity, invalidating the old map.
        db.info.setdefault('authorization_cache', {})[cache_key] = result
        return result
    return call

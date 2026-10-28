"""Trino catalogs for external data sources, generated per managed layer.

A PostgreSQL source with an engine login, or an Iceberg source with engine storage credentials,
gets its catalog written by the control plane (which holds the secret store) into the Secret
`fdp-source-catalogs` of that source's layer namespace only. The layer controller never
reads it: it receives a digest through the database and puts it on Trino's pod template, so a
change rolls Trino. Access-setup (provisioner) logins never reach the engine.

Sources whose catalog name is the layer's own PostgreSQL or Iceberg catalog stay bound to that
layer store and are not generated here.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Dict, Optional

from sqlalchemy import select, update

from .models import DataSource, LayerOperation, Resource, utcnow
from .secrets import datasource_path

SECRET = 'fdp-source-catalogs'
log = logging.getLogger(__name__)


class UnsafeCredential(ValueError):
    pass


def value(text) -> str:
    """A Java properties value Trino will read back verbatim."""
    text = str(text)
    if any(c in text for c in '\r\n\0'):
        raise UnsafeCredential('line breaks are not allowed in catalog settings')
    if '${' in text:  # Trino would substitute it (e.g. ${ENV:...}) and could leak pod secrets
        raise UnsafeCredential('"${" is not allowed in catalog settings')
    text = text.replace('\\', '\\\\')
    return '\\' + text if text[:1] in (' ', '\t', '\f') else text


def properties(source: DataSource, secret: Dict[str, str]) -> Optional[str]:
    """The catalog file for one source, or None when it has no engine credentials."""
    config = source.config or {}
    if source.kind == 'postgres' and secret.get('username') and secret.get('password'):
        url = 'jdbc:postgresql://%s:%s/%s' % (config['host'], config.get('port', 5432), config['database'])
        mode = config.get('sslmode')
        if mode:
            url += '?sslmode=' + mode
            if mode in ('verify-ca', 'verify-full'):
                # Use the layer's Java truststore (trino.tls.trust_bundle), with hostname checks for verify-full.
                url += '&sslfactory=org.postgresql.ssl.DefaultJavaSSLFactory'
        lines = {'connector.name': 'postgresql', 'connection-url': url,
                 'connection-user': secret['username'], 'connection-password': secret['password']}
    elif source.kind == 'iceberg' and secret.get('access_key_id') and secret.get('secret_access_key'):
        lines = {'connector.name': 'iceberg', 'iceberg.catalog.type': 'rest',
                 'iceberg.rest-catalog.uri': config['catalog_uri'], 'iceberg.rest-catalog.warehouse': config['warehouse'],
                 'iceberg.rest-catalog.vended-credentials-enabled': 'false', 'fs.s3.enabled': 'true',
                 's3.region': config.get('region', 'us-east-1'),
                 **({'s3.endpoint': config['endpoint_url'], 's3.path-style-access': 'true'} if config.get('endpoint_url') else {}),
                 's3.aws-access-key': secret['access_key_id'], 's3.aws-secret-key': secret['secret_access_key'],
                 # Access is decided by the platform's policy; these procedures could read arbitrary files.
                 'iceberg.security': 'SYSTEM', 'iceberg.register-table-procedure.enabled': 'false',
                 'iceberg.add-files-procedure.enabled': 'false'}
    else:
        return None
    return ''.join('%s=%s\n' % (key, value(v)) for key, v in lines.items())


def layer_bound_catalogs(layer: Resource) -> set:
    infrastructure = (layer.config or {}).get('infrastructure') or {}
    postgres, iceberg = infrastructure.get('postgres') or {}, infrastructure.get('iceberg') or {}
    return {c for c in (postgres.get('catalog') if postgres.get('enabled') else None,
                        iceberg.get('catalog') if iceberg.get('enabled') else None) if c}


def desired(db, store, layer: Resource) -> Dict[str, str]:
    """{catalog file name: properties} for the layer's external SQL sources with engine credentials."""
    from .layers import layer_of
    from .sql_access import SQL_KINDS, catalog_name
    bound, files = layer_bound_catalogs(layer), {}
    for source in db.scalars(select(DataSource).where(DataSource.kind.in_(SQL_KINDS)).order_by(DataSource.name)):
        config = source.config or {}
        if layer_of(db, config.get('layer_id') or config.get('environment_id')) != layer.id:
            continue
        catalog = catalog_name(source)
        if catalog in bound:
            continue
        try:
            text = properties(source, store.read(datasource_path(source.name)) or {})
        except UnsafeCredential as exc:
            log.warning('source %s: catalog not generated: %s', source.name, exc)
            continue
        if text:
            files[catalog + '.properties'] = text
    return files


def digest(files: Dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:32] if files else ''


def reconcile_layer(db, orch, store, layer: Resource) -> bool:
    """Write the layer's source catalogs if they changed; True when the Trino rollout was requested."""
    from .layer_infrastructure import namespace_for
    from .config import get_settings
    infrastructure = (layer.config or {}).get('infrastructure') or {}
    if not infrastructure.get('enabled') or infrastructure.get('retired'):
        return False
    namespace = namespace_for(layer.id, get_settings().layer_namespace_prefix)
    files = desired(db, store, layer)
    wanted = digest(files)
    if orch.layer_secret_digest(namespace, SECRET) != wanted:
        if not orch.write_layer_secret(namespace, SECRET, files, wanted):
            return False  # the namespace does not exist yet; the next pass writes it
    if (layer.config or {}).get('source_catalogs', '') == wanted:
        return False
    layer.config = {**layer.config, 'source_catalogs': wanted}
    # Reconcile now instead of at the next periodic check.
    db.execute(update(LayerOperation).where(LayerOperation.layer_id == layer.id,
               LayerOperation.generation == layer.config.get('infrastructure_generation', 0))
               .values(next_attempt_at=utcnow()).execution_options(synchronize_session=False))
    return True


def reconcile_all(factory, orch, store):
    with factory() as db:
        for layer in db.scalars(select(Resource).where(Resource.kind == 'layer')):
            try:
                if reconcile_layer(db, orch, store, layer):
                    log.info('layer %s: source catalogs changed; Trino will roll', layer.id)
                db.commit()
            except Exception:
                db.rollback()
                log.exception('source catalogs for layer %s failed; retrying', layer.id)


async def catalog_loop(factory, orch, store, interval: float = 30):
    import asyncio
    from starlette.concurrency import run_in_threadpool
    while True:
        try:
            await run_in_threadpool(reconcile_all, factory, orch, store)
        except Exception:
            log.exception('source catalog reconciliation failed')
        try:  # layer namespaces appear over time: give them the registry pull secret too
            from .registry_credentials import sync_once

            def pull_secrets():
                with factory() as db:
                    return sync_once(db, orch)
            for namespace in await run_in_threadpool(pull_secrets):
                log.info('registry pull secret written to %s', namespace)
        except Exception:
            log.exception('registry pull secret sync failed')
        await asyncio.sleep(interval)

"""Optional single-instance layer data PostgreSQL, separate from platform metadata.

Credential values never enter these manifests. The bootstrap admin credential is
mounted only in the database pod; the Trino login is a separate, restricted role.
"""
import hashlib
import json

BOOTSTRAP = '''#!/bin/bash
set -euo pipefail
ENGINE_PASSWORD="$(cat /postgres-auth/engine-password)" psql -X -v ON_ERROR_STOP=1 --username postgres --dbname postgres <<'SQL'
\\getenv engine_password ENGINE_PASSWORD
CREATE ROLE fdp_engine LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD :'engine_password';
CREATE DATABASE fdp OWNER fdp_engine;
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;
REVOKE CONNECT ON DATABASE template1 FROM PUBLIC;
REVOKE ALL ON DATABASE fdp FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE fdp TO fdp_engine;
SQL
'''

RELOAD = '''#!/bin/bash
set -euo pipefail
last=''
while sleep 5; do
  revision=$(readlink /tls-input/..data) || continue
  if [ "$revision" != "$last" ]; then
    if psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres -c 'SELECT pg_reload_conf()' >/dev/null 2>&1; then
      last="$revision"
      touch /tmp/postgres-tls-ready
    fi
  fi
done
'''

# Extra databases owned by the engine role, created idempotently so layers that existed
# before a feature was enabled get them too (initdb scripts run only once).
SETUP = '''#!/bin/bash
set -euo pipefail
# The role exists once initdb's bootstrap ran; a restart repeats this safely if the server
# was still the temporary initdb one.
until [ "$(psql -X -U postgres -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname = 'fdp_engine'" 2>/dev/null)" = 1 ]; do sleep 2; done
for db in "$@"; do
  psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres -v db="$db" <<'SQL'
SELECT format('CREATE DATABASE %I OWNER fdp_engine', :'db') WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db')\\gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'db')\\gexec
SELECT format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO fdp_engine', :'db')\\gexec
SQL
  # Optional per-database schema, applied as the engine role so it owns the objects.
  if [ -f "/etc/fdp-postgres/$db.sql" ]; then
    psql -X -v ON_ERROR_STOP=1 -U postgres -d "$db" -c 'SET ROLE fdp_engine' -f "/etc/fdp-postgres/$db.sql"
  fi
done
touch /tmp/postgres-setup-ready
exec sleep infinity
'''

# Iceberg's JDBC catalog tables (schema V1, with iceberg_type for views). Trino requires
# them to exist; it does not create them.
SCHEMAS = {'iceberg': '''CREATE TABLE IF NOT EXISTS iceberg_namespace_properties (
  catalog_name VARCHAR(255) NOT NULL, namespace VARCHAR(255) NOT NULL,
  property_key VARCHAR(255), property_value VARCHAR(1000),
  PRIMARY KEY (catalog_name, namespace, property_key));
CREATE TABLE IF NOT EXISTS iceberg_tables (
  catalog_name VARCHAR(255) NOT NULL, table_namespace VARCHAR(255) NOT NULL, table_name VARCHAR(255) NOT NULL,
  metadata_location VARCHAR(1000), previous_metadata_location VARCHAR(1000), iceberg_type VARCHAR(5),
  PRIMARY KEY (catalog_name, table_namespace, table_name));
'''}


def manifests(database, namespace, obj, databases=()):
    tls = database.tls.mode != 'disabled'
    hostname = 'fdp-postgres.%s.svc' % namespace
    docs = []
    if database.tls.mode == 'cert_manager':
        policy = database.tls
        docs.append(obj('Certificate', 'fdp-postgres', {'spec': {
            'secretName': policy.secret_name, 'dnsNames': [hostname],
            'duration': '%dh' % policy.duration_hours, 'renewBefore': '%dh' % policy.renew_before_hours,
            'privateKey': {'rotationPolicy': 'Always'}, 'issuerRef': policy.issuer.model_dump(),
            'usages': ['digital signature', 'key encipherment', 'server auth'],
        }}, 'cert-manager.io/v1'))
    hba = 'local all all peer\n'
    if tls:
        hba += 'hostnossl all all 0.0.0.0/0 reject\nhostnossl all all ::/0 reject\n'
    hba += '\n'.join('%s all all %s scram-sha-256' % ('hostssl' if tls else 'host', cidr)
                     for cidr in ('0.0.0.0/0', '::/0'))+'\n'
    config_data = {'pg_hba.conf': hba, '10-engine.sh': BOOTSTRAP, 'tls-reload.sh': RELOAD}
    if databases:
        config_data['setup.sh'] = SETUP
        config_data.update({db + '.sql': SCHEMAS[db] for db in databases if db in SCHEMAS})
    docs.append(obj('ConfigMap', 'fdp-postgres', {'data': config_data}))
    docs.append(obj('PersistentVolumeClaim', 'fdp-postgres', {'spec': {
        'accessModes': ['ReadWriteOnce'],
        **({'storageClassName': database.storage_class} if database.storage_class else {}),
        'resources': {'requests': {'storage': database.size}}}}))
    docs[-1]['metadata']['annotations'] = {'fdp.floren.app/retention': 'Retain'}
    labels = {**docs[-1]['metadata']['labels'], 'app.kubernetes.io/component': 'postgres'}
    docs.append(obj('Service', 'fdp-postgres', {'spec': {
        'selector': labels, 'ports': [{'name': 'postgres', 'port': 5432, 'targetPort': 'postgres'}]}}))
    volumes = [
        {'name': 'data', 'persistentVolumeClaim': {'claimName': 'fdp-postgres'}},
        {'name': 'socket', 'emptyDir': {'medium': 'Memory'}},
        {'name': 'config', 'configMap': {'name': 'fdp-postgres'}},
        {'name': 'bootstrap', 'configMap': {'name': 'fdp-postgres',
            'items': [{'key': '10-engine.sh', 'path': '10-engine.sh'}]}},
        {'name': 'auth', 'secret': {'secretName': database.credentials_secret, 'defaultMode': 0o640,
            'items': [{'key': key, 'path': key} for key in ('postgres-password', 'engine-password')]}},
    ]
    security = {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}}
    mounts = [{'name': 'data', 'mountPath': '/var/lib/postgresql/data'},
              {'name': 'socket', 'mountPath': '/var/run/postgresql'},
              {'name': 'config', 'mountPath': '/etc/fdp-postgres', 'readOnly': True},
              {'name': 'bootstrap', 'mountPath': '/docker-entrypoint-initdb.d', 'readOnly': True},
              {'name': 'auth', 'mountPath': '/postgres-auth', 'readOnly': True}]
    args = ['postgres', '-c', 'password_encryption=scram-sha-256',
            '-c', 'hba_file=/etc/fdp-postgres/pg_hba.conf']
    sidecars = []
    if tls:
        volumes.append({'name': 'tls-input', 'secret': {'secretName': database.tls.secret_name,
            'defaultMode': 0o640, 'items': [{'key': key, 'path': key} for key in ('tls.key', 'tls.crt')]}})
        mounts.append({'name': 'tls-input', 'mountPath': '/tls-input', 'readOnly': True})
        args += ['-c', 'ssl=on', '-c', 'ssl_cert_file=/tls-input/tls.crt', '-c', 'ssl_key_file=/tls-input/tls.key']
        sidecars.append({'name': 'tls-reload', 'image': database.image, 'securityContext': security,
            'command': ['bash', '/etc/fdp-postgres/tls-reload.sh'],
            'volumeMounts': [mount for mount in mounts if mount['name'] in ('socket', 'config', 'tls-input')],
            'resources': {'requests': {'cpu': '10m', 'memory': '32Mi'}, 'limits': {'cpu': '100m', 'memory': '128Mi'}},
            'readinessProbe': {'exec': {'command': ['test', '-f', '/tmp/postgres-tls-ready']}, 'periodSeconds': 5}})
    if databases:
        sidecars.append({'name': 'setup', 'image': database.image, 'securityContext': security,
            'command': ['bash', '/etc/fdp-postgres/setup.sh', *databases],
            'volumeMounts': [mount for mount in mounts if mount['name'] in ('socket', 'config')],
            'resources': {'requests': {'cpu': '10m', 'memory': '32Mi'}, 'limits': {'cpu': '100m', 'memory': '128Mi'}},
            'readinessProbe': {'exec': {'command': ['test', '-f', '/tmp/postgres-setup-ready']}, 'periodSeconds': 5}})
    probe = {'exec': {'command': ['psql', '-X', '-v', 'ON_ERROR_STOP=1', '-U', 'postgres', '-d', 'postgres', '-tAc', 'SELECT 1']},
             'timeoutSeconds': 5, 'periodSeconds': 5}
    docs.append(obj('Deployment', 'fdp-postgres', {'spec': {
        'replicas': 1, 'strategy': {'type': 'Recreate'}, 'progressDeadlineSeconds': 600,
        'selector': {'matchLabels': labels}, 'template': {'metadata': {'labels': labels, 'annotations': {
            'fdp.floren.app/config-hash': hashlib.sha256(json.dumps(config_data, sort_keys=True).encode()).hexdigest()}},
            'spec': {'serviceAccountName': 'fdp-execution', 'automountServiceAccountToken': False,
                'securityContext': {'runAsNonRoot': True, 'runAsUser': 999, 'runAsGroup': 999, 'fsGroup': 999,
                    'seccompProfile': {'type': 'RuntimeDefault'}}, 'volumes': volumes,
                'containers': [{'name': 'postgres', 'image': database.image, 'args': args, 'securityContext': security,
                    'ports': [{'name': 'postgres', 'containerPort': 5432}], 'volumeMounts': mounts,
                    'env': [{'name': 'PGDATA', 'value': '/var/lib/postgresql/data/pgdata'},
                        {'name': 'POSTGRES_PASSWORD_FILE', 'value': '/postgres-auth/postgres-password'},
                        {'name': 'POSTGRES_INITDB_ARGS', 'value': '--auth-host=scram-sha-256 --auth-local=peer'}],
                    'resources': {kind: {'cpu': '%dm' % round(database.cpu*1000), 'memory': database.memory}
                                  for kind in ('requests', 'limits')},
                    'startupProbe': {**probe, 'failureThreshold': 120}, 'readinessProbe': probe}, *sidecars]}}}}, 'apps/v1'))
    return docs

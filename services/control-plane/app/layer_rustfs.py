"""Optional single-instance layer RustFS (S3-compatible object storage).

Credential values never enter these manifests: the server and its bucket sidecar read the
namespace-local credentials Secret (keys access-key and secret-key) from files.
"""
COMPONENT = 'rustfs'


def manifests(store, namespace, obj, helper_image, trust=None):
    """trust: the layer's CA bundle reference (trino.tls.trust_bundle); required when TLS is on."""
    tls = store.tls.mode != 'disabled'
    hostname = 'fdp-rustfs.%s.svc' % namespace
    docs = []
    if store.tls.mode == 'cert_manager':
        policy = store.tls
        docs.append(obj('Certificate', 'fdp-rustfs', {'spec': {
            'secretName': policy.secret_name, 'dnsNames': [hostname],
            'duration': '%dh' % policy.duration_hours, 'renewBefore': '%dh' % policy.renew_before_hours,
            'privateKey': {'rotationPolicy': 'Always'}, 'issuerRef': policy.issuer.model_dump(),
            'usages': ['digital signature', 'key encipherment', 'server auth'],
        }}, 'cert-manager.io/v1'))
    docs.append(obj('PersistentVolumeClaim', 'fdp-rustfs', {'spec': {
        'accessModes': ['ReadWriteOnce'],
        **({'storageClassName': store.storage_class} if store.storage_class else {}),
        'resources': {'requests': {'storage': store.size}}}}))
    docs[-1]['metadata']['annotations'] = {'fdp.floren.app/retention': 'Retain'}
    labels = {**docs[-1]['metadata']['labels'], 'app.kubernetes.io/component': COMPONENT}
    docs.append(obj('Service', 'fdp-rustfs', {'spec': {
        'selector': labels, 'ports': [{'name': 's3', 'port': 9000, 'targetPort': 's3'}]}}))
    security = {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}}
    auth = {'name': 'auth', 'mountPath': '/rustfs-auth', 'readOnly': True}
    server = {'name': 'rustfs', 'image': store.image, 'securityContext': security,
        'ports': [{'name': 's3', 'containerPort': 9000}],
        'env': [{'name': 'RUSTFS_VOLUMES', 'value': '/data'},
                {'name': 'RUSTFS_ADDRESS', 'value': '0.0.0.0:9000'},
                {'name': 'RUSTFS_CONSOLE_ENABLE', 'value': 'false'},
                {'name': 'RUSTFS_ACCESS_KEY_FILE', 'value': '/rustfs-auth/access-key'},
                {'name': 'RUSTFS_SECRET_KEY_FILE', 'value': '/rustfs-auth/secret-key'},
                {'name': 'RUSTFS_OBS_LOG_DIRECTORY', 'value': '/logs'}],
        'volumeMounts': [{'name': 'data', 'mountPath': '/data'}, {'name': 'logs', 'mountPath': '/logs'}, auth],
        'resources': {kind: {'cpu': '%dm' % round(store.cpu * 1000), 'memory': store.memory}
                      for kind in ('requests', 'limits')},
        'startupProbe': {'tcpSocket': {'port': 's3'}, 'periodSeconds': 5, 'failureThreshold': 60},
        'readinessProbe': {'tcpSocket': {'port': 's3'}, 'periodSeconds': 5}}
    buckets = {'name': 'buckets', 'image': helper_image, 'securityContext': security,
        'command': ['python', '-m', 'app.rustfs_buckets', '--credentials', '/rustfs-auth',
                    '--ready', '/tmp/buckets-ready',
                    # The service name resolves to this pod (hostAliases), so the real certificate name is checked.
                    *(['--endpoint', 'https://%s:9000' % hostname, '--ca', '/trust/ca.pem'] if tls else []),
                    *store.buckets],
        'volumeMounts': [auth, *([{'name': 'trust', 'mountPath': '/trust', 'readOnly': True}] if tls else [])],
        'resources': {'requests': {'cpu': '10m', 'memory': '64Mi'}, 'limits': {'cpu': '100m', 'memory': '128Mi'}},
        'readinessProbe': {'exec': {'command': ['test', '-f', '/tmp/buckets-ready']}, 'periodSeconds': 5}}
    volumes = [{'name': 'data', 'persistentVolumeClaim': {'claimName': 'fdp-rustfs'}},
               {'name': 'logs', 'emptyDir': {}},
               {'name': 'auth', 'secret': {'secretName': store.credentials_secret, 'defaultMode': 0o440,
                   'items': [{'key': key, 'path': key} for key in ('access-key', 'secret-key')]}}]
    containers, pod_extra = [server, buckets], {}
    if tls:
        # RustFS reads rustfs_cert.pem/rustfs_key.pem from RUSTFS_TLS_PATH at startup only.
        volumes += [{'name': 'tls', 'secret': {'secretName': store.tls.secret_name, 'defaultMode': 0o440, 'items': [
                        {'key': 'tls.crt', 'path': 'rustfs_cert.pem'}, {'key': 'tls.key', 'path': 'rustfs_key.pem'}]}},
                    {'name': 'trust', 'secret': {'secretName': trust.name, 'items': [{'key': trust.key, 'path': 'ca.pem'}]}}]
        server['env'].append({'name': 'RUSTFS_TLS_PATH', 'value': '/tls'})
        server['volumeMounts'].append({'name': 'tls', 'mountPath': '/tls', 'readOnly': True})
        # It does not reload a renewed certificate, so a sidecar restarts this container when one arrives.
        containers.append({'name': 'tls-restart', 'image': helper_image, 'securityContext': security,
            'command': ['python', '-m', 'app.tls_restart', '--watch', '/tls', '--process', 'rustfs'],
            'volumeMounts': [{'name': 'tls', 'mountPath': '/tls', 'readOnly': True}],
            'resources': {'requests': {'cpu': '10m', 'memory': '32Mi'}, 'limits': {'cpu': '100m', 'memory': '128Mi'}}})
        pod_extra = {'shareProcessNamespace': True, 'hostAliases': [{'ip': '127.0.0.1', 'hostnames': [hostname]}]}
    docs.append(obj('Deployment', 'fdp-rustfs', {'spec': {
        'replicas': 1, 'strategy': {'type': 'Recreate'}, 'progressDeadlineSeconds': 600,
        'selector': {'matchLabels': labels}, 'template': {'metadata': {'labels': labels},
            'spec': {'serviceAccountName': 'fdp-execution', 'automountServiceAccountToken': False,
                'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001, 'runAsGroup': 10001, 'fsGroup': 10001,
                    'seccompProfile': {'type': 'RuntimeDefault'}},
                **pod_extra, 'volumes': volumes, 'containers': containers}}}}, 'apps/v1'))
    return docs


def engine_env(store):
    """Trino reads the S3 keys from the same Secret through ${ENV:...} in its catalog files."""
    return [{'name': name, 'valueFrom': {'secretKeyRef': {'name': store.credentials_secret, 'key': key}}}
            for name, key in (('FDP_RUSTFS_ACCESS_KEY', 'access-key'), ('FDP_RUSTFS_SECRET_KEY', 'secret-key'))]

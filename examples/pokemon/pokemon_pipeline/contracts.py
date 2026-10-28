import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

NAMESPACE = uuid.UUID('12db4878-d7aa-4e14-b0e9-7135fdf00240')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def stable_id(*parts):
    return uuid.uuid5(NAMESPACE, json.dumps(parts, separators=(',', ':'))).hex


def batch_id(environment, run_id):
    if not run_id or len(run_id) > 250:
        raise ValueError('a pipeline run ID of 1–250 characters is required')
    return stable_id(environment, run_id)


def identifier(value):
    if not re.fullmatch('[a-z][a-z0-9_]{0,62}', value):
        raise ValueError('invalid SQL identifier')
    return value


def business(value):
    # Retrieval timestamps remain in raw JSON but do not create business versions.
    if isinstance(value, dict):
        return {k: business(v) for k, v in value.items() if k != 'updated'}
    if isinstance(value, list):
        return [business(v) for v in value]
    return value


def records(payload, entity_type):
    """Preserve complete JSON; additionally emit every object in nested object lists.

    Stable upstream IDs identify root records. Children use their own ID within their
    parent/path, or their ordinal when no upstream ID exists. JSON pointers address
    the exact object in the original file (including escaped object keys).
    """
    def escape(value):
        return str(value).replace('~', '~0').replace('/', '~1')

    def emit(obj, kind, pointer, parent=None, identity=None):
        if not isinstance(obj, dict):
            raise ValueError('root collections must contain objects')
        rid = stable_id(kind, str(obj['id'])) if parent is None and 'id' in obj else stable_id(parent, kind, identity)
        if parent is None and 'id' not in obj:
            raise ValueError('a root record must have a stable upstream id')
        yield {'record_id': rid, 'parent_record_id': parent, 'entity_type': kind,
               'entity_id': str(obj.get('id', identity or '')), 'json_pointer': pointer,
               'payload': encode(obj).decode(), 'content_hash': sha(encode(business(obj)))}

        def descend(value, path, suffix):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield from descend(child, path + '/' + escape(key), suffix + '.' + key)
            elif isinstance(value, list):
                keys = set()
                for index, child in enumerate(value):
                    child_path = path + '/' + str(index)
                    if isinstance(child, dict):
                        key = str(child.get('id', index))
                        if key in keys:
                            raise ValueError('duplicate child ID at ' + path)
                        keys.add(key)
                        yield from emit(child, kind + suffix, child_path, rid, key)
                    else:
                        yield from descend(child, child_path, suffix)

        yield from descend(obj, pointer, '')

    roots = payload if isinstance(payload, list) else [payload]
    seen = set()
    for index, obj in enumerate(roots):
        for row in emit(obj, entity_type, '/' + str(index) if isinstance(payload, list) else ''):
            if row['record_id'] in seen:
                raise ValueError('duplicate record ID in API response')
            seen.add(row['record_id'])
            yield row


def put_immutable(files, path, content):
    """Conditional S3 create: a retry may only reuse the exact same bytes."""
    from botocore.exceptions import ClientError
    try:
        files.client.put_object(Bucket=files.bucket, Key=files.key(path), Body=content,
                               ContentType='application/json', IfNoneMatch='*')
    except ClientError as exc:
        if str(exc.response.get('ResponseMetadata', {}).get('HTTPStatusCode')) != '412':
            raise
        if files.read_bytes(path) != content:
            raise ValueError('immutable object already exists with different content: ' + path) from None
    # The conditional write uses boto3 directly. Report the same file namespace
    # attribution as the SDK, without maintaining manual table lineage lists.
    from fdp.lake import file_lineage
    file_lineage.note(files.name, files.namespaces, path, 'outputs')
    return files.url(path)


def validate_manifest(manifest, environment, batch):
    if (manifest.get('version') not in (1, 2) or manifest.get('environment') != environment or
            manifest.get('batch_id') != batch or manifest.get('status') != 'complete'):
        raise ValueError('manifest version, environment, batch or completion state does not match')
    paths, ids = set(), set()
    for entry in manifest['files']:
        if (not entry['path'].startswith('landing/batches/' + batch + '/') or
                '..' in entry['path'].split('/') or entry['path'] in paths or entry['file_id'] in ids):
            raise ValueError('invalid or duplicate manifest file')
        if manifest['version'] == 2:
            responses = entry.get('responses', [])
            if (entry.get('format') != 'json-array' or entry.get('entity_type') not in ('cards', 'sets') or
                    not responses or entry.get('root_count') != len(responses)):
                raise ValueError('invalid array file metadata')
            for response in responses:
                if (not response.get('source_path', '').startswith('/' + entry['entity_type'] + '/') or
                        not isinstance(response.get('byte_offset'), int) or response['byte_offset'] < 1 or
                        not isinstance(response.get('byte_count'), int) or response['byte_count'] < 1 or
                        response['byte_offset'] + response['byte_count'] > entry['bytes'] - 1 or
                        not re.fullmatch('[a-f0-9]{64}', response.get('sha256', ''))):
                    raise ValueError('invalid response trace metadata')
        paths.add(entry['path']); ids.add(entry['file_id'])
    if not paths:
        raise ValueError('empty manifest')


def rows(result):
    return [dict(zip(result['columns'], row)) for row in result['rows']]

"""TCGdex managed API → immutable S3 files; no database access."""
import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fdp
from pokemon_pipeline.contracts import batch_id, encode, now, put_immutable, sha, stable_id, validate_manifest


MAX_LANDING_BYTES = 16 * 1024 * 1024

def get_response(api, path):
    for attempt in range(6):
        try:
            response = api.get(path, timeout=130)
            response.raise_for_status()
            response.json()  # validate before persisting a completion marker
            return response.content
        except Exception as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            if attempt == 5 or (status is not None and status != 429 and status < 500):
                raise
            time.sleep(min(30, 2 ** attempt) + random.random())


def extract(api, files, environment, run_id, set_ids):
    batch = batch_id(environment, run_id)
    manifest_path = f'landing/batches/{batch}/manifest.json'
    try:
        manifest = files.read_json(manifest_path)
    except Exception as exc:
        # Only a real object-not-found permits a new extraction. Never treat
        # forbidden, expired credentials or a network failure as absence.
        if getattr(exc, 'response', {}).get('Error', {}).get('Code') not in ('NoSuchKey', '404'):
            raise
    else:
        validate_manifest(manifest, environment, batch)
        return manifest
    if not set_ids or not all(re.fullmatch('[a-z0-9.-]+', sid) for sid in set_ids) or len(set_ids) != len(set(set_ids)):
        raise ValueError('invalid/duplicate set allowlist')
    observed = now()
    entries = []

    pending = {'sets': [], 'cards': []}
    sizes = {'sets': 2, 'cards': 2}  # JSON array brackets

    def flush(kind):
        if not pending[kind]:
            return
        # Original response bytes remain intact inside a multi-object JSON array.
        content = b'[' + b','.join(raw for raw, _ in pending[kind]) + b']'
        digest = sha(content)
        fid = stable_id(batch, kind, digest)
        key = f'landing/batches/{batch}/{kind}-{fid}.json'
        responses, offset = [], 1
        for raw, metadata in pending[kind]:
            responses.append({**metadata, 'byte_offset': offset, 'byte_count': len(raw)})
            offset += len(raw) + 1
        put_immutable(files, key, content)
        entries.append({'path': key, 'file_id': fid, 'sha256': digest, 'bytes': len(content),
                        'source_path': '/' + kind, 'entity_type': kind, 'format': 'json-array',
                        'root_count': len(responses), 'responses': responses,
                        'captured_at': now(), 'source_file_uri': files.url(key)})
        pending[kind], sizes[kind] = [], 2

    def capture(path, kind):
        content = get_response(api, path)
        payload = json.loads(content)
        if not isinstance(payload, dict) or 'id' not in payload:
            raise ValueError('API detail response must be an object with an ID')
        if len(content) + 2 > MAX_LANDING_BYTES:
            raise ValueError('API response exceeds landing file size limit')
        added = len(content) + bool(pending[kind])
        if sizes[kind] + added > MAX_LANDING_BYTES:
            flush(kind)
            added = len(content)
        pending[kind].append((content, {'source_path': path, 'sha256': sha(content), 'captured_at': now()}))
        sizes[kind] += added
        return payload

    for sid in set_ids:
        data = capture('/sets/' + sid, 'sets')
        if data.get('id') != sid:
            raise ValueError('set identity mismatch')
        cards = data.get('cards', [])
        ids = [card['id'] for card in cards]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError('empty or duplicate card inventory')
        # Conservative sequential requests avoid bursts against the public API.
        for cid in ids:
            if not re.fullmatch('[A-Za-z0-9.-]+', cid):
                raise ValueError('invalid card ID')
            card = capture('/cards/' + cid, 'cards')
            if card.get('id') != cid or card.get('set', {}).get('id') != sid:
                raise ValueError('card identity/inventory mismatch')
    for kind in ('sets', 'cards'):
        flush(kind)
    manifest = {'version': 2, 'status': 'complete', 'environment': environment, 'run_id': run_id,
                'batch_id': batch, 'observed_at': observed, 'completed_at': now(),
                'sets': set_ids, 'files': entries, 'extract_execution_id': os.getenv('FDP_RUN_ID', '')}
    put_immutable(files, manifest_path, encode(manifest))
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    result = extract(fdp.api(os.environ['POKEMON_API']), fdp.files(os.environ['POKEMON_LANDING']),
                     os.environ['POKEMON_ENV'], args.run_id, os.environ.get('POKEMON_SETS', 'base1,base2').split(','))
    print(json.dumps({'batch_id': result['batch_id'], 'files': len(result['files']), 'status': 'complete'}))


if __name__ == '__main__':
    main()

"""Immutable S3 manifests → append-only raw rows, files and completion markers."""
import argparse
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fdp
from pokemon_pipeline.contracts import batch_id, identifier, now, records, rows, sha, stable_id, validate_manifest

MAX_BATCH_ROWS = 100


def append(query, schema, table, columns, values):
    if not values:
        return
    # Small batches keep SQL/request sizes bounded, including large nested JSON.
    chunk, size = [], 0
    for row in values:
        n = sum(len(str(v)) for v in row)
        if n > 500_000:
            raise ValueError('raw record exceeds the 500 KB row limit')
        if chunk and (len(chunk) >= MAX_BATCH_ROWS or size + n > 500_000):
            _insert(query, schema, table, columns, chunk)
            chunk, size = [], 0
        chunk.append(row); size += n
    if chunk:
        _insert(query, schema, table, columns, chunk)


def _insert(query, schema, table, columns, chunk):
    placeholder = '(' + ','.join('?' for _ in columns) + ')'
    query(f"INSERT INTO {schema}.{table} ({','.join(columns)}) VALUES " + ','.join(placeholder for _ in chunk),
          [value for row in chunk for value in row])


def event(query, schema, batch, run_id, stage, status, load_id='', count=0, detail='', execution_id=None):
    append(query, schema, 'process_events',
           ['event_id','batch_id','pipeline_run_id','stage','status','event_at','load_id','record_count','execution_id','detail'],
           [[stable_id(batch, stage, status, load_id), batch, run_id, stage, status, now(), load_id, count,
             execution_id if execution_id is not None else os.getenv('FDP_RUN_ID', ''), detail]])


def load(files, query, schema, environment, run_id):
    schema = identifier(schema)
    batch = batch_id(environment, run_id)
    # One active DAG per environment; completed batches are idempotent on retry.
    if query(f'SELECT load_id FROM {schema}.load_commits WHERE batch_id=? LIMIT 1', [batch])['rows']:
        return {'batch_id': batch, 'status': 'already_loaded'}
    load_id = uuid.uuid4().hex
    event(query, schema, batch, run_id, 'load_raw', 'started', load_id)
    try:
        manifest = files.read_json(f'landing/batches/{batch}/manifest.json')
        validate_manifest(manifest, environment, batch)
        event(query, schema, batch, run_id, 'extract', 'succeeded', load_id, len(manifest['files']),
              'Complete S3 manifest verified', execution_id=manifest.get('extract_execution_id',''))
        total = 0
        file_counts = {}

        def record_rows():
            nonlocal total
            for entry in manifest['files']:
                content = files.read_bytes(entry['path'])
                if sha(content) != entry['sha256'] or len(content) != entry['bytes']:
                    raise ValueError('file checksum/size mismatch')
                payload = json.loads(content)
                if manifest['version'] == 2:
                    if not isinstance(payload, list) or len(payload) != entry['root_count']:
                        raise ValueError('file root count mismatch')
                    for index, response in enumerate(entry['responses']):
                        start, length = response['byte_offset'], response['byte_count']
                        original = content[start:start + length]
                        if sha(original) != response['sha256'] or json.loads(original) != payload[index]:
                            raise ValueError('response trace mismatch')
                observed = list(records(payload, entry['entity_type']))
                if manifest['version'] == 1 and len(observed) != entry['record_count']:
                    raise ValueError('file record count mismatch')
                file_counts[entry['file_id']] = len(observed)
                for row in observed:
                    source_path = (entry['responses'][int(row['json_pointer'].split('/')[1])]['source_path']
                                   if manifest['version'] == 2 else entry['source_path'])
                    yield [row['record_id'], stable_id(batch, entry['file_id'], row['json_pointer'], row['entity_type']),
                       row['parent_record_id'], row['entity_type'], row['entity_id'], row['json_pointer'],
                       row['payload'], row['content_hash'], batch, load_id, entry['file_id'],
                       entry['source_file_uri'], entry['sha256'], source_path, manifest['observed_at'], now()]
                    total += 1

        # Batch across file boundaries: one Iceberg commit per bounded chunk,
        # rather than two commits for every API response object.
        append(query, schema, 'records',
                   ['record_id','observation_id','parent_record_id','entity_type','entity_id','json_pointer',
                    'payload','content_hash','batch_id','load_id','file_id','source_file_uri','file_sha256',
                    'source_path','observed_at','loaded_at'], record_rows())
        append(query, schema, 'files',
                   ['file_id','batch_id','load_id','source_file_uri','file_sha256','source_path','entity_type','byte_count','record_count','captured_at'],
                   ([entry['file_id'],batch,load_id,entry['source_file_uri'],entry['sha256'],entry['source_path'],
                     entry['entity_type'],entry['bytes'],file_counts[entry['file_id']],entry['captured_at']]
                    for entry in manifest['files']))
        # A marker admits exactly one complete load attempt; partial attempts are
        # retained for diagnostics and never reach dbt. No raw UPDATE or DELETE.
        append(query, schema, 'load_commits',
               ['batch_id','load_id','pipeline_run_id','observed_at','committed_at','file_count','record_count'],
               [[batch,load_id,run_id,manifest['observed_at'],now(),len(manifest['files']),total]])
        event(query, schema, batch, run_id, 'load_raw', 'succeeded', load_id, total)
        return {'batch_id': batch, 'load_id': load_id, 'records': total, 'status': 'loaded'}
    except Exception as exc:
        # Do not persist credentials, payloads or arbitrary exception text.
        try:
            event(query, schema, batch, run_id, 'load_raw', 'failed', load_id, detail=type(exc).__name__)
        except Exception:
            pass
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    query = lambda sql, parameters=None: fdp.query(os.environ['POKEMON_SOURCE'], sql, parameters)
    print(json.dumps(load(fdp.files(os.environ['POKEMON_LANDING']), query,
                          os.getenv('POKEMON_RAW_SCHEMA','pokemon_raw'), os.environ['POKEMON_ENV'], args.run_id)))


if __name__ == '__main__':
    main()

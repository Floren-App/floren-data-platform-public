import copy
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pokemon_pipeline.contracts import records, sha, stable_id, encode, batch_id
from ingestion.extract import extract
from ingestion.load_raw import load
from ingestion.bootstrap import TABLES


class Files:
    bucket, prefix, name, namespaces = 'landing-test', 'pokemon/test', 'test-files', {'landing':'landing'}
    def __init__(self):
        self.objects = {}
        self.client = self
    def key(self, path):
        return self.prefix + '/' + path
    def url(self, path):
        return 's3://' + self.bucket + '/' + self.key(path)
    def put_object(self, Bucket, Key, Body, **kwargs):
        assert kwargs['IfNoneMatch'] == '*'
        if Key in self.objects:
            raise ClientError({'Error':{'Code':'PreconditionFailed'},'ResponseMetadata':{'HTTPStatusCode':412}},'PutObject')
        self.objects[Key] = Body
    def read_bytes(self, path):
        try: return self.objects[self.key(path)]
        except KeyError:
            raise ClientError({'Error':{'Code':'NoSuchKey'},'ResponseMetadata':{'HTTPStatusCode':404}},'GetObject')
    def read_json(self, path):
        return json.loads(self.read_bytes(path))


class Api:
    def __init__(self, name='Alakazam'):
        self.data = {'/sets/base1':{'id':'base1','name':'Base Set','cards':[{'id':'base1-1'}]},
                     '/cards/base1-1':{'id':'base1-1','name':name,'set':{'id':'base1'},
                                        'attacks':[{'name':'Thunder','damage':40}]}}
        self.calls = []
    def get(self, path, **kwargs):
        self.calls.append(path)
        content = encode(self.data[path])
        class Response:
            def json(self): return json.loads(content)
            def raise_for_status(self): pass
        response = Response(); response.content = content
        return response


class Database:
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute("ATTACH DATABASE ':memory:' AS pokemon_raw")
        self.db.create_function('from_iso8601_timestamp',1,lambda x:x)
        for table, columns in TABLES.items(): self.db.execute(f'CREATE TABLE pokemon_raw.{table} ({columns})')
        self.statements = []
    def __call__(self, sql, parameters=None):
        assert sql.lstrip().split()[0] in ('SELECT','INSERT'), 'loader must never mutate/delete/DDL raw'
        self.statements.append(sql)
        cur = self.db.execute(sql,parameters or [])
        return {'columns':[c[0] for c in cur.description] if cur.description else [],'rows':cur.fetchall()}
    def render(self, relative):
        template = Environment(undefined=StrictUndefined).from_string((ROOT/'dbt/pokemon'/relative).read_text())
        return template.render(ref=lambda x:x,source=lambda s,t:'pokemon_raw.'+t,config=lambda **kwargs:'')
    def models(self):
        for table, path in [('stg_records','staging'),('record_history','history')]:
            self.db.execute('DROP VIEW IF EXISTS '+table)
            self.db.execute('CREATE TEMP VIEW '+table+' AS '+self.render('models/'+path+'/'+table+'.sql'))


def test_nested_lists_preserve_parent_json_and_stable_ids():
    payload = [{'id':'x','nested/key':{'a~b':[{'id':'attack1','name':'Zap'},{'name':'Heal'}]},'types':['Electric']}]
    out = list(records(payload,'cards'))
    assert len(out) == 3
    assert json.loads(out[0]['payload']) == payload[0]
    assert out[1]['parent_record_id'] == out[0]['record_id']
    assert out[1]['json_pointer'] == '/0/nested~1key/a~0b/0'
    changed = copy.deepcopy(payload); changed[0]['nested/key']['a~b'][0]['name'] = 'Thunder'
    newer = list(records(changed,'cards'))
    assert newer[1]['record_id'] == out[1]['record_id']
    assert newer[1]['content_hash'] != out[1]['content_hash']
    with pytest.raises(ValueError): list(records([{'id':'x'},{'id':'x'}],'cards'))
    with pytest.raises(ValueError): list(records([{'name':'no id'}],'cards'))


def test_extract_retries_reuse_complete_immutable_manifest_and_exact_bytes():
    files, api = Files(), Api()
    first = extract(api,files,'test','run-1',['base1'])
    before = dict(files.objects)
    assert extract(api,files,'test','run-1',['base1']) == first
    assert len(api.calls) == 2 and files.objects == before
    for file in first['files']:
        content = files.read_bytes(file['path'])
        assert isinstance(json.loads(content), list)
        assert sha(content) == file['sha256']
        for response in file['responses']:
            start, length = response['byte_offset'], response['byte_count']
            assert content[start:start + length] == encode(api.data[response['source_path']])
            assert sha(content[start:start + length]) == response['sha256']


def test_loader_replay_file_trace_and_process_history():
    files, db = Files(), Database()
    manifest = extract(Api(),files,'test','run-1',['base1'])
    loaded = load(files,db,'pokemon_raw','test','run-1')
    assert loaded['records'] == 4 # set, brief card, detailed card, attack
    assert load(files,db,'pokemon_raw','test','run-1')['status'] == 'already_loaded'
    assert db('SELECT count(*) FROM pokemon_raw.records')['rows'] == [(4,)]
    db.models()
    assert db.db.execute('SELECT count(*) FROM stg_records').fetchone()[0] == 4
    assert not db.db.execute(db.render('tests/file_traceability.sql')).fetchall()
    assert not db.db.execute(db.render('tests/complete_inventory.sql')).fetchall()
    assert db("SELECT status FROM pokemon_raw.process_events WHERE stage='load_raw' ORDER BY event_at")['rows'] == [('started',),('succeeded',)]


def test_corrupt_file_never_commits_and_partial_attempt_does_not_enter_models(monkeypatch):
    monkeypatch.setattr('ingestion.load_raw.MAX_BATCH_ROWS', 1)
    files, db = Files(), Database()
    manifest = extract(Api(),files,'test','run-1',['base1'])
    key = files.key(manifest['files'][-1]['path'])
    original = files.objects[key]; files.objects[key] = b'{}'
    with pytest.raises(ValueError): load(files,db,'pokemon_raw','test','run-1')
    assert db('SELECT count(*) FROM pokemon_raw.load_commits')['rows'] == [(0,)]
    assert db('SELECT count(*) FROM pokemon_raw.records')['rows'][0][0] > 0
    db.models()
    assert db.db.execute('SELECT count(*) FROM stg_records').fetchone()[0] == 0
    files.objects[key] = original
    assert load(files,db,'pokemon_raw','test','run-1')['status'] == 'loaded'
    db.models()
    assert db.db.execute('SELECT count(*) FROM stg_records').fetchone()[0] == 4


def test_landing_batches_api_objects_and_loader_batches_database_inserts():
    files, db, api = Files(), Database(), Api()
    api.data['/sets/base1']['cards'] = [{'id':f'base1-{n}'} for n in range(50)]
    card = api.data.pop('/cards/base1-1')
    for n in range(50):
        api.data[f'/cards/base1-{n}'] = {**card,'id':f'base1-{n}'}
    manifest = extract(api,files,'test','batched',['base1'])
    assert manifest['version'] == 2
    assert len(manifest['files']) == 2 and len(files.objects) == 3
    card_file = next(f for f in manifest['files'] if f['entity_type'] == 'cards')
    payload = files.read_json(card_file['path'])
    assert len(payload) == 50 and all(isinstance(c['attacks'], list) for c in payload)
    assert card_file['root_count'] == 50
    result = load(files,db,'pokemon_raw','test','batched')
    assert result['records'] == 151
    assert len([s for s in db.statements if s.startswith('INSERT INTO pokemon_raw.records ')]) == 2
    assert len([s for s in db.statements if s.startswith('INSERT INTO pokemon_raw.files ')]) == 1


def test_cross_environment_manifest_is_refused():
    files, db = Files(), Database()
    manifest = extract(Api(),files,'test','run-1',['base1'])
    manifest['environment'] = 'prod'
    files.objects[files.key('landing/batches/'+manifest['batch_id']+'/manifest.json')] = encode(manifest)
    with pytest.raises(ValueError): load(files,db,'pokemon_raw','test','run-1')
    assert db('SELECT count(*) FROM pokemon_raw.load_commits')['rows'] == [(0,)]


def test_real_scd2_sql_collapses_unchanged_and_preserves_a_b_a_with_all_file_evidence():
    files, db = Files(), Database()
    for index, name in enumerate(['Alakazam','Alakazam','Pikachu','Alakazam']):
        run = 'run-'+str(index)
        extract(Api(name),files,'test',run,['base1'])
        load(files,db,'pokemon_raw','test',run)
    db.models()
    history = db.db.execute("SELECT record_id,version_number,payload,valid_to,is_current,observation_count "
                            "FROM record_history WHERE entity_type='cards' ORDER BY version_number").fetchall()
    assert len(history) == 3
    assert len({row[0] for row in history}) == 1
    assert [json.loads(row[2])['name'] for row in history] == ['Alakazam','Pikachu','Alakazam']
    assert [row[4] for row in history] == [0,0,1]
    assert history[0][5] == 2
    assert db.db.execute("SELECT count(*) FROM stg_records WHERE entity_type='cards'").fetchone()[0] == 4
    assert not db.db.execute(db.render('tests/scd2_intervals.sql')).fetchall()
    assert not db.db.execute(db.render('tests/file_traceability.sql')).fetchall()


def test_missing_manifest_permissions_are_not_treated_as_not_found():
    files = Files()
    def forbidden(path): raise ClientError({'Error':{'Code':'AccessDenied'}},'GetObject')
    files.read_json = forbidden
    with pytest.raises(ClientError): extract(Api(),files,'test','run-1',['base1'])
    assert not files.objects


def test_workspace_schema_override_cannot_write_shared_reporting():
    from types import SimpleNamespace
    text = (ROOT/'dbt/pokemon/macros/generate_schema_name.sql').read_text()
    template = Environment(undefined=StrictUndefined).from_string(text)
    for schema, expected in [('dbt_ana','dbt_ana'),('pokemon_history','reporting')]:
        module = template.make_module({'target':SimpleNamespace(schema=schema)})
        assert module.generate_schema_name('reporting',None).strip() == expected


def test_every_dbt_sql_model_parses_as_trino():
    import sqlglot
    db = Database()
    for folder in ('models','tests'):
        for path in (ROOT/'dbt/pokemon'/folder).rglob('*.sql'):
            sqlglot.parse_one(db.render(str(path.relative_to(ROOT/'dbt/pokemon'))),read='trino')


def test_landing_preserves_original_response_bytes_in_array(monkeypatch):
    api, files, db = Api(), Files(), Database()
    get = api.get
    def spaced(*args, **kwargs):
        response = get(*args, **kwargs)
        response.content = b' \n' + response.content + b'\n '
        return response
    monkeypatch.setattr(api, 'get', spaced)
    manifest = extract(api, files, 'test', 'spaced', ['base1'])
    for entry in manifest['files']:
        content = files.read_bytes(entry['path'])
        for response in entry['responses']:
            start, size = response['byte_offset'], response['byte_count']
            assert content[start:start + size] == b' \n' + encode(api.data[response['source_path']]) + b'\n '
    assert load(files, db, 'pokemon_raw', 'test', 'spaced')['records'] == 4


def test_bounded_array_parts_keep_record_ids_and_exact_file_pointers(monkeypatch):
    monkeypatch.setattr('ingestion.extract.MAX_LANDING_BYTES', 350)
    api, files, db = Api(), Files(), Database()
    api.data['/sets/base1']['cards'] = [{'id': f'base1-{i}'} for i in range(8)]
    card = api.data.pop('/cards/base1-1')
    for i in range(8):
        api.data[f'/cards/base1-{i}'] = {**card, 'id': f'base1-{i}'}
    manifest = extract(api, files, 'test', 'parts', ['base1'])
    assert len([f for f in manifest['files'] if f['entity_type'] == 'cards']) > 1
    assert all(f['bytes'] <= 350 for f in manifest['files'])
    assert load(files, db, 'pokemon_raw', 'test', 'parts')['records'] == 25
    by_uri = {f['source_file_uri']: files.read_json(f['path']) for f in manifest['files']}
    result = db('SELECT record_id,source_file_uri,json_pointer,payload,source_path,entity_type FROM pokemon_raw.records')
    for rid, uri, pointer, payload, source_path, kind in result['rows']:
        value = by_uri[uri]
        for segment in pointer.split('/')[1:]:
            key = segment.replace('~1', '/').replace('~0', '~')
            value = value[int(key)] if isinstance(value, list) else value[key]
        assert value == json.loads(payload)
        if kind == 'cards':
            assert rid == stable_id('cards', value['id'])
            assert source_path == '/cards/' + value['id']
    db.models()
    assert not db.db.execute(db.render('tests/complete_inventory.sql')).fetchall()
    assert not db.db.execute(db.render('tests/file_traceability.sql')).fetchall()


def test_loader_accepts_legacy_single_response_manifest_without_rewriting_files():
    api, files, db = Api(), Files(), Database()
    batch = batch_id('test', 'legacy')
    entries = []
    for path, payload in api.data.items():
        kind = 'cards' if path.startswith('/cards/') else 'sets'
        content = encode(payload)
        fid = stable_id(batch, path, sha(content))
        key = f'landing/batches/{batch}/{fid}.json'
        files.objects[files.key(key)] = content
        entries.append({'path': key, 'file_id': fid, 'sha256': sha(content), 'bytes': len(content),
                        'source_path': path, 'entity_type': kind, 'record_count': len(list(records(payload, kind))),
                        'captured_at': '2026-01-01T00:00:00Z', 'source_file_uri': files.url(key)})
    manifest = {'version': 1, 'status': 'complete', 'environment': 'test', 'batch_id': batch,
                'observed_at': '2026-01-01T00:00:00Z', 'files': entries}
    files.objects[files.key(f'landing/batches/{batch}/manifest.json')] = encode(manifest)
    before = dict(files.objects)
    assert extract(api, files, 'test', 'legacy', ['base1']) == manifest
    assert api.calls == [] and files.objects == before
    assert load(files, db, 'pokemon_raw', 'test', 'legacy')['records'] == 4
    db.models()
    assert not db.db.execute(db.render('tests/file_traceability.sql')).fetchall()


def test_wrong_response_mapping_never_commits_batch():
    files, db = Files(), Database()
    manifest = extract(Api(), files, 'test', 'bad-trace', ['base1'])
    manifest['files'][0]['responses'][0]['sha256'] = '0' * 64
    files.objects[files.key(f"landing/batches/{manifest['batch_id']}/manifest.json")] = encode(manifest)
    with pytest.raises(ValueError, match='response trace mismatch'):
        load(files, db, 'pokemon_raw', 'test', 'bad-trace')
    assert db('SELECT count(*) FROM pokemon_raw.load_commits')['rows'] == [(0,)]

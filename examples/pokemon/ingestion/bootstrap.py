"""One-time, idempotent table setup. Separate identity; never drops existing data."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fdp
from pokemon_pipeline.contracts import identifier

TABLES = {
    'records': '''record_id varchar, observation_id varchar, parent_record_id varchar,
        entity_type varchar, entity_id varchar, json_pointer varchar, payload varchar,
        content_hash varchar, batch_id varchar, load_id varchar, file_id varchar,
        source_file_uri varchar, file_sha256 varchar, source_path varchar,
        observed_at varchar, loaded_at varchar''',
    'files': '''file_id varchar, batch_id varchar, load_id varchar, source_file_uri varchar,
        file_sha256 varchar, source_path varchar, entity_type varchar, byte_count bigint,
        record_count bigint, captured_at varchar''',
    'load_commits': '''batch_id varchar, load_id varchar, pipeline_run_id varchar, observed_at varchar,
        committed_at varchar, file_count bigint, record_count bigint''',
    'process_events': '''event_id varchar, batch_id varchar, pipeline_run_id varchar, stage varchar,
        status varchar, event_at varchar, load_id varchar, record_count bigint, execution_id varchar, detail varchar''',
}


def bootstrap(query, raw, history, product):
    for schema in (raw, history, product):
        query('CREATE SCHEMA IF NOT EXISTS ' + identifier(schema))
    for name, columns in TABLES.items():
        query(f'CREATE TABLE IF NOT EXISTS {raw}.{name} ({columns})')


if __name__ == '__main__':
    bootstrap(lambda sql: fdp.query(os.environ['POKEMON_SOURCE'], sql),
              os.getenv('POKEMON_RAW_SCHEMA','pokemon_raw'),
              os.getenv('POKEMON_HISTORY_SCHEMA','pokemon_history'),
              os.getenv('POKEMON_PRODUCT_SCHEMA','reporting'))

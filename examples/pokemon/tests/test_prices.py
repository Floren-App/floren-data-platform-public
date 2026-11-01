"""Failure/replay and cross-environment tests for the Kafka -> table boundary."""
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from streaming.prices import COLUMNS, DDL, land_poll, quotes, validate


class Consumer:
    def __init__(self, events):
        self.records = [SimpleNamespace(value=event, topic='pokemon-card-prices-test', partition=0, offset=i)
                        for i, event in enumerate(events)]
        self.commits = 0

    def poll(self, **kwargs):
        return {0: self.records}

    def commit(self):
        self.commits += 1


def events(run='demo-1'):
    return list(quotes('test', run, '2026-11-01T12:00:00Z'))


def database():
    db = sqlite3.connect(':memory:')
    db.execute("ATTACH DATABASE ':memory:' AS pokemon_prices")
    db.execute(f'CREATE TABLE pokemon_prices.price_events ({DDL})')
    return db


def test_quotes_stable_on_retry_and_distinct_between_layers():
    assert events() == events()
    assert events() != events('demo-2')
    assert events()[0]['event_id'] != list(quotes('prod', 'demo-1', '2026-11-01T12:00:00Z'))[0]['event_id']
    for event in events():
        assert validate(event, 'test') == event
        assert event['price_source'] == 'synthetic_demo'


@pytest.mark.parametrize('field,value', [('environment','prod'), ('currency','USD'), ('price_minor',True),
    ('price_minor',-1), ('price_minor',1.2), ('price_source','live'), ('version',True),
    ('observed_at','2026-11-01T12:00:00'), ('card_name','Unexpected private name')])
def test_poison_poll_is_never_written_or_committed(field, value):
    batch = events()
    batch[1][field] = value
    consumer = Consumer(batch)
    writes = []
    with pytest.raises((ValueError, TypeError)):
        land_poll(consumer, lambda *args: writes.append(args), 'pokemon_prices', 'test', 'pokemon-card-prices-test')
    assert not writes and consumer.commits == 0


def test_sql_failure_does_not_commit_offsets():
    consumer = Consumer(events())
    def failed(*args):
        raise RuntimeError('SQL unavailable')
    with pytest.raises(RuntimeError):
        land_poll(consumer, failed, 'pokemon_prices', 'test', 'pokemon-card-prices-test')
    assert consumer.commits == 0


def test_replay_is_deduplicated_in_materialized_product():
    db = database()
    consumer = Consumer(events())
    def write(sql, values):
        db.execute(sql, values)
        db.commit()
    for _ in range(2):
        assert land_poll(consumer, write, 'pokemon_prices', 'test', 'pokemon-card-prices-test') == 3
    assert consumer.commits == 2
    assert db.execute('SELECT count(*) FROM pokemon_prices.price_events').fetchone()[0] == 6
    template = Environment(undefined=StrictUndefined).from_string(
        (ROOT/'dbt/prices/models/card_prices_current.sql').read_text())
    sql = template.render(config=lambda **kw:'', env_var=lambda k,d:d, source=lambda *a:'pokemon_prices.price_events')
    rows = db.execute(sql).fetchall()
    assert len(rows) == 3
    # A new producer run supersedes the previous quote, even if transport repeats.
    newer = list(quotes('test', 'demo-2', '2026-11-01T12:02:00Z'))
    consumer = Consumer(newer)
    for r in consumer.records:
        r.offset += 3
    land_poll(consumer, write, 'pokemon_prices', 'test', 'pokemon-card-prices-test')
    rows = db.execute(sql).fetchall()
    assert len(rows) == 3
    assert {r[0] for r in rows} == {e['event_id'] for e in newer}


def test_commit_response_loss_replays_safely():
    db = database()
    consumer = Consumer(events())
    def commit_failure():
        raise RuntimeError('commit response lost')
    consumer.commit = commit_failure
    with pytest.raises(RuntimeError):
        land_poll(consumer, lambda sql, args: db.execute(sql, args), 'pokemon_prices', 'test', 'pokemon-card-prices-test')
    assert db.execute('SELECT count(*) FROM pokemon_prices.price_events').fetchone()[0] == 3


def test_empty_poll_does_not_commit_or_query():
    consumer = Consumer([])
    assert land_poll(consumer, lambda *a: pytest.fail('unexpected SQL'), 'pokemon_prices', 'test', 'pokemon-card-prices-test') == 0
    assert consumer.commits == 0


def test_partial_batch_failure_keeps_rows_and_does_not_commit():
    db = database()
    # More than one insert chunk, so a failure can follow successful durable rows.
    consumer = Consumer((events() * 20))
    calls = []
    def write(sql, values):
        calls.append(sql)
        if len(calls) == 2:
            raise RuntimeError('second insert failed')
        db.execute(sql, values)
        db.commit()
    with pytest.raises(RuntimeError):
        land_poll(consumer, write, 'pokemon_prices', 'test', 'pokemon-card-prices-test')
    assert len(calls) == 2
    assert consumer.commits == 0
    assert db.execute('SELECT count(*) FROM pokemon_prices.price_events').fetchone()[0] == 50


def test_wrong_topic_cannot_land_in_this_table():
    consumer = Consumer(events())
    consumer.records[1].topic = 'pokemon-card-prices-prod'
    with pytest.raises(ValueError):
        land_poll(consumer, lambda *a: pytest.fail('must validate before writing'),
                  'pokemon_prices', 'test', 'pokemon-card-prices-test')
    assert consumer.commits == 0


def test_publisher_accepts_platform_cli_flags(monkeypatch):
    import runpy
    import fdp
    sent = []
    class Producer:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def send(self, **kwargs):
            sent.append(kwargs)
            return SimpleNamespace(get=lambda **kwargs: None)
    monkeypatch.syspath_prepend(str(ROOT/'streaming'))
    monkeypatch.setenv('POKEMON_ENV', 'test')
    monkeypatch.setenv('POKEMON_PRICE_TOPIC', 'pokemon-card-prices-test')
    monkeypatch.setattr(fdp.kafka, 'producer', lambda topic: Producer())
    monkeypatch.setattr(sys, 'argv', ['publish.py', '--run-id', 'demo-1', '--observed-at', '2026-11-01T12:00:00Z'])
    runpy.run_path(str(ROOT/'streaming/publish.py'), run_name='__main__')
    assert [row['value'] for row in sent] == events()


def test_readiness_cli_uses_same_ids_as_publisher(monkeypatch):
    import runpy
    import fdp
    monkeypatch.syspath_prepend(str(ROOT/'streaming'))
    monkeypatch.setenv('POKEMON_ENV', 'test')
    monkeypatch.setenv('POKEMON_PRICE_SCHEMA', 'pokemon_prices')
    monkeypatch.setenv('POKEMON_SOURCE', 'lake')
    calls = []
    def query(source, sql, parameters):
        calls.append(parameters)
        return {'rows': [[value] for value in parameters]}
    monkeypatch.setattr(fdp, 'query', query)
    monkeypatch.setattr(sys, 'argv', ['await_quotes.py', '--run-id', 'demo-1', '--observed-at', '2026-11-01T12:00:00Z'])
    runpy.run_path(str(ROOT/'streaming/await_quotes.py'), run_name='__main__')
    assert calls == [sorted(event['event_id'] for event in events())]


def test_price_sql_parses_for_trino():
    import sqlglot
    text = (ROOT/'dbt/prices/models/card_prices_current.sql').read_text()
    sql = Environment(undefined=StrictUndefined).from_string(text).render(
        config=lambda **kw:'', env_var=lambda k,d:d, source=lambda *a:'pokemon_prices.price_events')
    assert sqlglot.parse_one(sql, read='trino') is not None

"""Synthetic, non-personal quote contract and durable Kafka-to-SQL ingestion.

Delivery is at least once. Canonical dbt models remove retry duplicates. No Kafka
commit happens until every record in a poll has been validated and stored.
"""
import hashlib
import json
import re
from datetime import datetime, timezone

CARDS = {'base1-4': 'Charizard', 'base1-58': 'Pikachu', 'base1-2': 'Blastoise'}
COLUMNS = ('event_id', 'card_id', 'card_name', 'observed_at', 'price_minor', 'currency',
           'price_source', 'environment', 'topic', 'partition_id', 'offset_id', 'ingested_at')
DDL = '''event_id varchar, card_id varchar, card_name varchar, observed_at varchar,
price_minor bigint, currency varchar, price_source varchar, environment varchar,
topic varchar, partition_id integer, offset_id bigint, ingested_at varchar'''


def table_name(schema):
    if not re.fullmatch('[a-z][a-z0-9_]{0,62}', schema):
        raise ValueError('Invalid price schema')
    return schema + '.price_events'


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError('observed_at must be UTC')
    return parsed.isoformat(timespec='microseconds')


def quotes(environment, run_id, observed_at):
    if environment not in ('test', 'prod') or not run_id or len(run_id) > 256:
        raise ValueError('Invalid environment/run ID')
    observed_at = timestamp(observed_at)
    for card_id, card_name in CARDS.items():
        digest = hashlib.sha256(json.dumps([environment, run_id, card_id]).encode()).hexdigest()
        yield {'version': 1, 'event_id': digest, 'card_id': card_id, 'card_name': card_name,
               'observed_at': observed_at, 'price_minor': 100 + int(digest[:8], 16) % 50000,
               'currency': 'EUR', 'price_source': 'synthetic_demo', 'environment': environment}


def validate(event, environment):
    if not isinstance(event, dict) or set(event) != {
        'version', 'event_id', 'card_id', 'card_name', 'observed_at', 'price_minor',
        'currency', 'price_source', 'environment'
    }:
        raise ValueError('Invalid quote fields')
    if type(event['version']) is not int or event['version'] != 1:
        raise ValueError('Unknown quote version')
    if (event['environment'] != environment or environment not in ('test', 'prod')
            or event['currency'] != 'EUR' or event['price_source'] != 'synthetic_demo'
            or event['card_id'] not in CARDS or event['card_name'] != CARDS[event['card_id']]):
        raise ValueError('Quote does not match this demo contract/layer')
    if type(event['price_minor']) is not int or not 0 <= event['price_minor'] <= 100000000:
        raise ValueError('Invalid integer price')
    if not isinstance(event['event_id'], str) or not re.fullmatch('[0-9a-f]{64}', event['event_id']):
        raise ValueError('Invalid event ID')
    if timestamp(event['observed_at']) != event['observed_at']:
        raise ValueError('Non-canonical timestamp')
    return event


def land_poll(consumer, query, schema, environment, topic):
    table = table_name(schema)
    records = [record for batch in consumer.poll(timeout_ms=1000, max_records=100).values() for record in batch]
    rows = []
    for record in records:
        event = validate(record.value, environment)
        if record.topic != topic or record.partition < 0 or record.offset < 0:
            raise ValueError('Unexpected Kafka provenance')
        rows.append(tuple(event[key] for key in COLUMNS[:8]) + (
            record.topic, record.partition, record.offset, datetime.now(timezone.utc).isoformat()))
    # Validate the whole poll first; bound inserts to limit request size. Replaying
    # an ambiguous SQL response is safe for the canonical model, not exactly once.
    for start in range(0, len(rows), 50):
        chunk = rows[start:start + 50]
        values = ','.join('(' + ','.join('?' for _ in COLUMNS) + ')' for _ in chunk)
        query(f'INSERT INTO {table} ({",".join(COLUMNS)}) VALUES {values}',
              [value for row in chunk for value in row])
    if rows:
        consumer.commit()
    return len(rows)

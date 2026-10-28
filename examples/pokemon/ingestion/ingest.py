"""Complete, append-only English TCGdex observations via governed API + SQL gateways.

Only committed batches enter dbt. Failed/incomplete batches remain auditable but invisible.
No data credentials or access tokens are persisted in the repository.
"""
import hashlib
import json
import os
import random
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import fdp


def canonical(value):
    """Ignore upstream retrieval/pricing timestamps, retain actual business attributes/prices."""
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in value.items() if k != 'updated'}
    if isinstance(value, list):
        return [canonical(v) for v in value]
    return value


def observation(payload):
    text = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    digest = hashlib.sha256(json.dumps(canonical(payload), sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    return text, digest


def get_json(api, path):
    for attempt in range(6):
        try:
            response = api.get(path, timeout=130)
            if response.status_code == 429 or response.status_code >= 500:
                response.raise_for_status()
            response.raise_for_status()
            return response.json()
        except Exception:
            if attempt == 5:
                raise
            time.sleep(min(30, 2 ** attempt) + random.random())


def main():
    source = os.environ['POKEMON_SOURCE']
    api = fdp.api(os.environ['POKEMON_API'])
    set_ids = os.environ.get('POKEMON_SETS', 'base1,base2').split(',')
    if not all(re.fullmatch('[a-z0-9.-]+', s) for s in set_ids):
        raise ValueError('invalid set allowlist')
    sets, cards = [], []
    for sid in set_ids:
        data = get_json(api, '/sets/' + sid)
        brief = data.pop('cards')
        ids = [c['id'] for c in brief]
        if len(ids) != len(set(ids)) or not ids:
            raise ValueError('empty/duplicate card inventory for ' + sid)
        with ThreadPoolExecutor(max_workers=3) as pool:
            records = list(pool.map(lambda cid: get_json(api, '/cards/' + cid), ids))
        if {c['id'] for c in records} != set(ids) or any(c['set']['id'] != sid for c in records):
            raise ValueError('card inventory mismatch for ' + sid)
        sets.append(data)
        cards.extend(records)
        print(f"Fetched {data['name']}: {len(records)} English cards", flush=True)
    # Fetch and validate everything before writing. A single marker commits the whole batch.
    batch = uuid.uuid4().hex
    observed = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=' ', timespec='microseconds')
    for table, records in [('set_observations', sets), ('card_observations', cards)]:
        rows = []
        for record in records:
            payload, digest = observation(record)
            sid = record['set']['id'] if 'set' in record else record['id']
            rows.append([batch, record['id'], sid, payload, digest, observed])
        for offset in range(0, len(rows), 20):
            chunk = rows[offset:offset + 20]
            values = ','.join('(?,?,?,?,?,CAST(? AS timestamp(6)))' for _ in chunk)
            fdp.query(source, f'INSERT INTO raw.{table} (batch_id,entity_id,set_id,payload,content_hash,observed_at) VALUES {values}',
                            [value for row in chunk for value in row])
    fdp.query(source, 'INSERT INTO raw.ingestion_batches (batch_id,observed_at,card_count,set_count) VALUES (?,CAST(? AS timestamp(6)),?,?)',
                    [batch, observed, len(cards), len(sets)])
    print(json.dumps({'batch_id': batch, 'cards': len(cards), 'sets': len(sets), 'environment':os.environ['POKEMON_ENV']}), flush=True)
    output = os.environ.get('FDP_OUTPUT_DIR')
    if output:
        with open(os.path.join(output, 'ingestion.json'), 'w') as f:
            json.dump({'batch_id': batch, 'cards':len(cards), 'sets':len(sets), 'observed_at':observed}, f)


if __name__ == '__main__':
    main()

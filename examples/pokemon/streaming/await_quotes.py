"""Bounded readiness check: never publish a successful mart before this run landed."""
import argparse
import os
import time
import fdp
from prices import quotes, table_name

parser = argparse.ArgumentParser()
parser.add_argument('--run-id', required=True)
parser.add_argument('--observed-at', required=True)
args = parser.parse_args()
expected = {event['event_id'] for event in quotes(os.environ['POKEMON_ENV'], args.run_id, args.observed_at)}
table = table_name(os.environ['POKEMON_PRICE_SCHEMA'])
for attempt in range(30):
    result = fdp.query(os.environ['POKEMON_SOURCE'],
                       f'SELECT DISTINCT event_id FROM {table} WHERE event_id IN (?,?,?)',
                       parameters=sorted(expected))
    if {row[0] for row in result['rows']} == expected:
        break
    if attempt == 29:
        raise TimeoutError('Quotes not durable after 30 checks; inspect the continuous price sink')
    time.sleep(2)

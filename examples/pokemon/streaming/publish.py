"""Bounded, explicitly synthetic EUR quotes; retries reuse run ID and logical UTC time."""
import argparse
import os
import fdp
from prices import quotes

parser = argparse.ArgumentParser()
parser.add_argument('--run-id', required=True)
parser.add_argument('--observed-at', required=True)
args = parser.parse_args()
with fdp.kafka.producer(os.environ['POKEMON_PRICE_TOPIC']) as producer:
    for event in quotes(os.environ['POKEMON_ENV'], args.run_id, args.observed_at):
        producer.send(value=event, key=event['card_id'].encode()).get(timeout=30)

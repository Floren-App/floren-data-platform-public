"""Continuous Kafka consumer. Poison events stop the workload without committing."""
import os
import fdp
from prices import land_poll

topic = os.environ['POKEMON_PRICE_TOPIC']
with fdp.kafka.consumer(topic, group='pokemon-price-table', auto_offset_reset='earliest') as consumer:
    while True:
        land_poll(consumer, lambda sql, values: fdp.query(os.environ['POKEMON_SOURCE'], sql, parameters=values),
                  os.environ['POKEMON_PRICE_SCHEMA'], os.environ['POKEMON_ENV'], topic)

"""One-time DDL identity; the continuous sink cannot create or drop tables."""
import os
import fdp
from prices import DDL, table_name

schema = os.environ['POKEMON_PRICE_SCHEMA']
table = table_name(schema)
fdp.query(os.environ['POKEMON_SOURCE'], f'CREATE SCHEMA IF NOT EXISTS {schema}')
fdp.query(os.environ['POKEMON_SOURCE'], f'CREATE TABLE IF NOT EXISTS {table} ({DDL})')

market = os.environ['POKEMON_PRICE_PRODUCT_SCHEMA']
table_name(market)  # validate identifier before interpolating DDL
fdp.query(os.environ['POKEMON_SOURCE'], f'CREATE SCHEMA IF NOT EXISTS {market}')

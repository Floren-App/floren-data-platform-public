{{ config(materialized='table', schema=env_var('POKEMON_PRICE_PRODUCT_SCHEMA', 'pokemon_market'), tags=['prices']) }}
-- Materialize under the builder identity: consumers never need raw-stream access.
with transport_deduplicated as (
    select *, row_number() over (
        partition by topic, partition_id, offset_id order by ingested_at
    ) as transport_rank
    from {{ source('pokemon_prices', 'price_events') }}
), latest as (
    select *, row_number() over (
        partition by card_id order by observed_at desc, event_id desc, topic, partition_id, offset_id
    ) as price_rank
    from transport_deduplicated where transport_rank = 1
)
select event_id, card_id, card_name, observed_at, price_minor,
       cast(price_minor as double) / 100.0 as price_eur, currency, price_source,
       environment, topic, partition_id, offset_id
from latest where price_rank = 1

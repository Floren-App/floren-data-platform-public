-- Sales from the Iceberg table, typed and cleaned.
select
    id                    as sale_id,
    cast(sold_at as date) as sold_on,
    lower(region)         as region,
    product,
    amount_eur
from {{ source('lake', 'sales') }}

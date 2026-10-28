-- Cleaned sales: one row per sale, typed and named consistently.
select
    id                      as sale_id,
    cast(sold_at as date)           as sold_on,
    lower(region)           as region,
    product,
    cast(amount_eur as decimal(12, 2))     as amount_eur
from {{ source('warehouse', 'sales') }}

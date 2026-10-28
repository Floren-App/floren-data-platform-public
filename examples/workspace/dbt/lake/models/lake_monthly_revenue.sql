-- Monthly revenue per region, as an Iceberg table other engines can read.
select
    date_trunc('month', sold_on) as month,
    region,
    count(*)                     as sales,
    round(sum(amount_eur), 2)    as revenue_eur
from {{ ref('stg_lake_sales') }}
group by 1, 2

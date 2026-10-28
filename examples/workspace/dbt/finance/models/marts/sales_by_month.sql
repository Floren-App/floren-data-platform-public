-- Monthly revenue per region and product.
select
    cast(date_trunc('month', sold_on) as date)  as month,
    region,
    product,
    count(*)                            as sales,
    round(sum(amount_eur), 2)           as revenue_eur
from {{ ref('stg_sales') }}
group by 1, 2, 3

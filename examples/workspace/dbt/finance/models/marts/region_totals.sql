-- Revenue per region over everything in sales_by_month.
select
    region,
    sum(sales)        as sales,
    sum(revenue_eur)  as revenue_eur
from {{ ref('sales_by_month') }}
group by 1

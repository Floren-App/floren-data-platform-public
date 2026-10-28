select set_id,set_name,series_name,release_date,count(*) as card_count,
       count(distinct rarity) as rarity_count,avg(hit_points) as average_hp,
       round(sum(coalesce(price_trend_eur,0)),2) as collection_trend_eur
from {{ ref('cards_current') }}
group by set_id,set_name,series_name,release_date

-- Explicit product contract: no raw payloads, internal file paths or process logs.
select c.record_id, c.record_version_id, c.card_id, c.set_id, c.card_name, c.card_number,
       c.category, c.rarity, c.hit_points, c.primary_type, c.evolution_stage, c.illustrator,
       c.image_url, c.price_trend_eur, c.standard_legal, c.valid_from as last_changed_at,
       c.last_seen_at, s.set_name, s.series_name, s.release_date
from {{ ref('dim_cards_history') }} c
join {{ ref('dim_sets_history') }} s on s.set_id = c.set_id and s.is_current
where c.is_current

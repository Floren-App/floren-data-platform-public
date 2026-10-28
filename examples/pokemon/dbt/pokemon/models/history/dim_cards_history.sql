select h.*, entity_id as card_id,
       json_extract_scalar(payload, '$.set.id') as set_id,
       json_extract_scalar(payload, '$.name') as card_name,
       json_extract_scalar(payload, '$.localId') as card_number,
       json_extract_scalar(payload, '$.category') as category,
       json_extract_scalar(payload, '$.rarity') as rarity,
       try_cast(json_extract_scalar(payload, '$.hp') as integer) as hit_points,
       json_extract_scalar(payload, '$.types[0]') as primary_type,
       json_extract_scalar(payload, '$.stage') as evolution_stage,
       json_extract_scalar(payload, '$.illustrator') as illustrator,
       json_extract_scalar(payload, '$.image') as image_url,
       try_cast(json_extract_scalar(payload, '$.pricing.cardmarket.trend') as double) as price_trend_eur,
       coalesce(try_cast(json_extract_scalar(payload, '$.legal.standard') as boolean), false) as standard_legal
from {{ ref('record_history') }} h where entity_type = 'cards'

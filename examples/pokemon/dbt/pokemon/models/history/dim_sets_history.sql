select h.*, entity_id as set_id,
       json_extract_scalar(payload, '$.name') as set_name,
       json_extract_scalar(payload, '$.serie.name') as series_name,
       try_cast(json_extract_scalar(payload, '$.releaseDate') as date) as release_date,
       try_cast(json_extract_scalar(payload, '$.cardCount.official') as integer) as official_card_count,
       json_extract_scalar(payload, '$.logo') as logo_url
from {{ ref('record_history') }} h where entity_type = 'sets'

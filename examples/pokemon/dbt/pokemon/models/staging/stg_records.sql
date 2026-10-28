-- Admit only the first complete attempt. Partial attempts stay auditable in raw.
with commits as (
    select *, row_number() over (partition by batch_id order by committed_at, load_id) as attempt_rank
    from {{ source('tcgdex', 'load_commits') }}
), observations as (
    select r.*, row_number() over (partition by r.observation_id order by r.loaded_at, r.load_id) as observation_rank
    from {{ source('tcgdex', 'records') }} r
    join commits c on c.batch_id = r.batch_id and c.load_id = r.load_id and c.attempt_rank = 1
)
select record_id, observation_id, parent_record_id, entity_type, entity_id, json_pointer,
       payload, content_hash, batch_id, load_id, file_id, source_file_uri, file_sha256,
       source_path, from_iso8601_timestamp(observed_at) as observed_at,
       from_iso8601_timestamp(loaded_at) as loaded_at
from observations where observation_rank = 1

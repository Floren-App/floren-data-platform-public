-- A→B→A creates three versions. Unchanged observations retain all file evidence
-- in stg_records while updating last_seen without another business version.
-- Missing upstream IDs are not inferred deletions.
with ordered as (
    select *, lag(content_hash) over (
        partition by record_id order by observed_at, batch_id, observation_id) as previous_hash
    from {{ ref('stg_records') }}
), groups as (
    select *, sum(case when previous_hash is null or previous_hash <> content_hash then 1 else 0 end)
        over (partition by record_id order by observed_at, batch_id, observation_id rows unbounded preceding) as version_number
    from ordered
), versions as (
    select *, row_number() over (
        partition by record_id, version_number order by observed_at, batch_id, observation_id) as first_observation,
        max(observed_at) over (partition by record_id, version_number) as last_seen_at,
        count(*) over (partition by record_id, version_number) as observation_count
    from groups
), intervals as (
    select *, lead(observed_at) over (
        partition by record_id order by observed_at, batch_id, observation_id) as valid_to
    from versions where first_observation = 1
)
select record_id, observation_id as record_version_id, parent_record_id, entity_type, entity_id,
       payload, content_hash, version_number, observed_at as valid_from, valid_to,
       valid_to is null as is_current, last_seen_at, observation_count,
       observation_id, batch_id, load_id, file_id, source_file_uri, file_sha256, source_path, json_pointer
from intervals

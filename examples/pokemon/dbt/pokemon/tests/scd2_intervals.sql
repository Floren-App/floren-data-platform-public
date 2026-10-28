{{ config(tags=['engineering']) }}
with intervals as (
    select *, lead(valid_from) over (partition by record_id order by version_number) as next_from,
           lag(content_hash) over (partition by record_id order by version_number) as previous_hash,
           sum(case when is_current then 1 else 0 end) over (partition by record_id) as current_count
    from {{ ref('record_history') }}
)
select record_id from intervals where current_count <> 1
   or (valid_to is not null and valid_to < valid_from)
   or (next_from is not null and valid_to <> next_from)
   or (next_from is null and valid_to is not null)
   or content_hash = previous_hash

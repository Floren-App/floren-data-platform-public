{{ config(tags=['engineering']) }}
select r.observation_id from {{ ref('stg_records') }} r
left join {{ source('tcgdex','files') }} f
  on f.batch_id = r.batch_id and f.load_id = r.load_id and f.file_id = r.file_id
where f.file_id is null or r.file_sha256 <> f.file_sha256 or r.source_file_uri <> f.source_file_uri

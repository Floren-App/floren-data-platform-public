{{ config(tags=['engineering']) }}
with counts as (
    select batch_id, load_id, count(*) as records, count(distinct file_id) as files
    from {{ source('tcgdex', 'records') }} group by batch_id, load_id
), declared_files as (
    select batch_id, load_id, sum(record_count) as records, count(*) as files
    from {{ source('tcgdex', 'files') }} group by batch_id, load_id
)
select c.batch_id from {{ source('tcgdex', 'load_commits') }} c
left join counts r on r.batch_id = c.batch_id and r.load_id = c.load_id
left join declared_files f on f.batch_id = c.batch_id and f.load_id = c.load_id
where coalesce(r.records,0) <> c.record_count or coalesce(r.files,0) <> c.file_count
   or coalesce(f.records,0) <> c.record_count or coalesce(f.files,0) <> c.file_count

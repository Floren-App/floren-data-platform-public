{{ config(materialized='table') }}

select
    cast(load_id as varchar) as _load_id,
    cast(source_file as varchar) as _source_file,
    cast(line_number as varchar) as _line_number,
    cast(record_length as varchar) as _record_length,
    cast(ingested_at as varchar) as _ingested_at,
    cast(definition_name as varchar) as _definition_name,
    {{ generate_parser(definition_name()) }}
from {{ source('landing', 'raw_lines') }}
where definition_name = '{{ definition_name() | replace("'", "''") }}'

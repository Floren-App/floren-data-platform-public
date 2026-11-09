-- Run before parsing: rejects short/long records and unknown definitions.
select
    raw.load_id,
    raw.source_file,
    raw.line_number,
    length(raw.raw_line) as actual_length,
    layout.expected_length
from {{ source('landing', 'raw_lines') }} as raw
left join (
    select definition_name, max(start_position + field_length - 1) as expected_length
    from {{ ref('file_definition') }}
    group by definition_name
) as layout on raw.definition_name = layout.definition_name
where raw.definition_name = '{{ definition_name() | replace("'", "''") }}'
  and (raw.raw_line is null or layout.expected_length is null or length(raw.raw_line) <> layout.expected_length)

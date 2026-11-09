{% macro generate_parser(definition_name) %}

    {% set query %}
        select column_name, start_position, field_length
        from {{ ref('file_definition') }}
        where definition_name = '{{ definition_name }}'
        order by start_position
    {% endset %}

    {% if execute %}
        {% set results = run_query(query) %}

        {% for row in results.rows %}
            nullif(
                trim(substr(raw_line, {{ row[1] }}, {{ row[2] }})),
                ''
            ) as {{ adapter.quote(row[0]) }}
            {% if not loop.last %},{% endif %}
        {% endfor %}

    {% else %}
        raw_line as _unparsed_preview
    {% endif %}

{% endmacro %}

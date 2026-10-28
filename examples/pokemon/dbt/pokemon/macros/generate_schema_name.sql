{% macro generate_schema_name(custom_schema_name, node) -%}
  {# Workspace builds remain private, including their product models. #}
  {%- if target.schema.startswith('dbt_') or custom_schema_name is none -%}
    {{ target.schema }}
  {%- else -%}
    {{ custom_schema_name | trim }}
  {%- endif -%}
{%- endmacro %}

{% macro definition_name() %}
    {#- The caller (the DAG) chooses the layout to parse; the project has no default. A run without it fails, but
        `dbt docs generate` compiles every node without the run's vars, so docs are generated without a layout. -#}
    {%- set name = var('definition_name', '') -%}
    {%- if execute and not name and flags.WHICH != 'generate' -%}
        {{ exceptions.raise_compiler_error("Pass the layout to parse, e.g. --vars '{definition_name: customers_v1}'") }}
    {%- endif -%}
    {{ return(name) }}
{% endmacro %}

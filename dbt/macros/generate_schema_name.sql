{#
    by default dbt glues the target dataset onto any custom schema, so a model
    configured with schema "pulseops_mart" lands in "pulseops_staging_mart".
    that is sensible when every developer needs their own sandbox, and wrong
    here, because terraform already created the four datasets and those are the
    ones the pipeline is supposed to write to.

    this override takes the custom schema literally and falls back to the
    profile's dataset when a model does not specify one.
#}

{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema | trim }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}

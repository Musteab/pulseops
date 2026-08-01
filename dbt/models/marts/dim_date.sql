-- a plain calendar. generated rather than seeded, because a date spine you can
-- regenerate for any window beats a csv that quietly runs out in 2027.
--
-- the range is deliberately wider than the data so late arrivals and backfills
-- still find a row to join to.

with spine as (

    select day
    from unnest(generate_date_array('2023-01-01', '2028-12-31', interval 1 day)) as day

)

select
    {{ dbt_utils.generate_surrogate_key(['day']) }} as date_key,
    day as date_day,
    extract(year from day) as year,
    extract(quarter from day) as quarter,
    extract(month from day) as month,
    format_date('%B', day) as month_name,
    extract(day from day) as day_of_month,
    extract(dayofweek from day) as day_of_week,
    format_date('%A', day) as day_name,
    -- bigquery counts sunday as 1, so the weekend is 1 and 7
    extract(dayofweek from day) in (1, 7) as is_weekend
from spine

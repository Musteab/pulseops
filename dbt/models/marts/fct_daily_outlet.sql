-- one row per outlet per day: what was sold, what was in stock, what the
-- weather did.
--
-- this is the model the batch sources exist for. orders alone tell you revenue
-- fell; orders joined to stock and weather tell you whether you ran out of
-- rendang or whether it rained all afternoon.
--
-- left joins on purpose. weather can be missing because a free api had a bad
-- morning, and inventory can be missing for a day the batch never ran. a day
-- with sales and no weather is still a real day, and inner joining would make
-- it vanish from the dashboard without telling anyone.

{{
    config(
        materialized = 'table',
        partition_by = {'field': 'order_date', 'data_type': 'date'},
        cluster_by = ['outlet_id']
    )
}}

with sales as (

    select
        order_date,
        outlet_id,
        count(distinct event_id) as orders,
        sum(qty) as units_sold,
        round(sum(captured_revenue_myr), 2) as captured_revenue_myr,
        round(sum(line_total_myr), 2) as gross_line_total_myr
    from {{ ref('fct_order_line') }}
    group by 1, 2

),

stock as (

    select
        snapshot_date,
        outlet_id,
        sum(units_on_hand) as units_on_hand,
        countif(units_on_hand = 0) as lines_out_of_stock
    from {{ source('raw', 'inventory_raw') }}
    group by 1, 2

),

weather as (

    -- weather arrives per city, and two outlets share Kuala Lumpur, so this
    -- fans out to outlets through the dimension rather than being joined
    -- directly. joining on city at the fact would silently double revenue.
    select
        w.weather_date,
        o.outlet_id,
        w.temp_max_c,
        w.precipitation_mm
    from {{ source('raw', 'weather_raw') }} as w
    join {{ ref('dim_outlet') }} as o on o.city = w.city

)

select
    sales.order_date,
    sales.outlet_id,
    sales.orders,
    sales.units_sold,
    sales.captured_revenue_myr,
    sales.gross_line_total_myr,

    stock.units_on_hand,
    stock.lines_out_of_stock,

    weather.temp_max_c,
    weather.precipitation_mm,
    -- a crude but honest threshold. "wet" is a business definition, not a
    -- meteorological one, and it belongs in the model rather than in whatever
    -- each dashboard author decides on the day.
    weather.precipitation_mm >= 10.0 as was_wet_day

from sales
left join stock
    on sales.order_date = stock.snapshot_date
   and sales.outlet_id = stock.outlet_id
left join weather
    on sales.order_date = weather.weather_date
   and sales.outlet_id = weather.outlet_id

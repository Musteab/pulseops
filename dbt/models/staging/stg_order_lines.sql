-- one row per line item, which is the grain everything downstream cares about.
--
-- questions worth asking are per item: which dish drove the drop, which
-- category carries the margin. rolling line grain up to order grain is cheap,
-- splitting order grain back down is impossible, so we model at the finer one.

with orders as (

    select
        event_id,
        order_id,
        outlet_id,
        channel,
        event_ts,
        payment_status,
        payment_method,
        lines
    from {{ ref('stg_orders') }}

),

exploded as (

    select
        orders.event_id,
        orders.order_id,
        orders.outlet_id,
        orders.channel,
        orders.event_ts,
        orders.payment_status,
        orders.payment_method,

        -- offset gives a stable position within the order, which combined with
        -- event_id forms the unique key for this grain
        line_position,

        json_value(line, '$.menu_item_id')                  as menu_item_id,
        json_value(line, '$.name')                          as menu_item_name,
        cast(json_value(line, '$.qty') as int64)            as qty,
        cast(json_value(line, '$.unit_price_myr') as numeric) as unit_price_myr,
        cast(json_value(line, '$.line_total_myr') as numeric) as line_total_myr

    from orders,
    unnest(json_query_array(orders.lines)) as line with offset as line_position

)

select
    {{ dbt_utils.generate_surrogate_key(['event_id', 'line_position']) }} as order_line_key,
    *
from exploded

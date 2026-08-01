-- the centre of the star. one row per line item on one order.
--
-- partitioned by date and clustered by outlet because essentially every query
-- against this table filters by day and groups by outlet, and the difference
-- between reading one partition and reading the whole table is the difference
-- between a free query and a bill.
--
-- foreign keys are joined here rather than looked up at query time, which is
-- what makes the orphan test meaningful: a menu_item_id with no dimension row
-- ends up with a null key, and the relationships test catches it.

{{
    config(
        materialized = 'table',
        partition_by = {'field': 'order_date', 'data_type': 'date'},
        cluster_by = ['outlet_key']
    )
}}

with lines as (

    select * from {{ ref('stg_order_lines') }}

),

outlets as (
    select outlet_key, outlet_id from {{ ref('dim_outlet') }}
),

menu_items as (
    select menu_item_key, menu_item_id from {{ ref('dim_menu_item') }}
),

dates as (
    select date_key, date_day from {{ ref('dim_date') }}
)

select
    lines.order_line_key,

    -- degenerate dimensions, kept on the fact because they identify the
    -- transaction and have nothing else worth describing them
    lines.event_id,
    lines.order_id,
    lines.line_position,

    outlets.outlet_key,
    -- an order line referencing an item that is not on the menu attaches to the
    -- unknown member rather than carrying a null. the row survives, the join
    -- holds, and dq_warehouse_faults counts how many ended up here.
    coalesce(menu_items.menu_item_key, '{{ var("unknown_key") }}') as menu_item_key,
    dates.date_key,

    -- natural keys kept alongside the surrogate ones. an orphan has a null
    -- menu_item_key but still shows you which id caused it, which is the
    -- difference between a failing test and a useful failing test.
    lines.outlet_id,
    lines.menu_item_id,
    lines.menu_item_name,

    cast(lines.event_ts as date) as order_date,
    lines.event_ts,
    lines.channel,
    lines.payment_status,

    lines.qty,
    lines.unit_price_myr,
    lines.line_total_myr,

    -- failed authorisations still happened as orders, they just did not earn
    -- anything. splitting the measure keeps "orders placed" and "money taken"
    -- from being silently conflated in every dashboard downstream.
    case when lines.payment_status = 'captured' then lines.line_total_myr else 0 end
        as captured_revenue_myr

from lines
left join outlets     on lines.outlet_id    = outlets.outlet_id
left join menu_items  on lines.menu_item_id = menu_items.menu_item_id
left join dates       on cast(lines.event_ts as date) = dates.date_day

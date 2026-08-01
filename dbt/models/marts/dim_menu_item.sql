-- the menu, plus an unknown member.
--
-- 17 order lines in the current data point at MI-9999, an item that was never
-- on the menu. there are three things you can do with those, and only one is
-- right:
--
--   drop them          revenue silently goes missing, nobody finds out
--   leave a null key   the fact table can no longer promise a valid join, and
--                      every downstream query has to remember to handle it
--   unknown member     the row survives, the join holds, and the damage is
--                      countable
--
-- so we keep a single sentinel row. orphaned lines attach to it, the fact table
-- stays complete, and dq_warehouse_faults can report exactly how many landed
-- there. a data quality problem you can put a number on is a problem you can
-- actually fix.

with known as (

    select
        {{ dbt_utils.generate_surrogate_key(['menu_item_id']) }} as menu_item_key,
        menu_item_id,
        name as menu_item_name,
        category,
        unit_price_myr as list_price_myr,
        is_vegetarian,
        false as is_unknown_member
    from {{ ref('dim_menu_item_seed') }}

),

unknown_member as (

    select
        '{{ var("unknown_key") }}' as menu_item_key,
        'UNKNOWN' as menu_item_id,
        'Unknown menu item' as menu_item_name,
        'unknown' as category,
        cast(null as numeric) as list_price_myr,
        cast(null as boolean) as is_vegetarian,
        true as is_unknown_member

)

select * from known
union all
select * from unknown_member

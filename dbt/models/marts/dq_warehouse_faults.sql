-- the warehouse layer's scorecard.
--
-- the contract catches everything visible in a single record and reports
-- 185/185. these are the three fault classes it structurally cannot see, each
-- needing something a lone record does not have: the other deliveries, the
-- menu, or a sense of when the window closed.
--
-- counting them here is what turns "the rest are handled downstream" from a
-- convenient claim into a number you can check against the manifest.

{{ config(materialized = 'view') }}

with redelivered as (

    select
        'duplicate_event' as fault_type,
        'needs the other records' as why_ingest_cannot_see_it,
        count(*) as events_affected
    from {{ ref('stg_orders') }}
    where was_redelivered

),

orphans as (

    select
        'orphan_menu_item' as fault_type,
        'needs the menu' as why_ingest_cannot_see_it,
        count(distinct event_id) as events_affected
    from {{ ref('fct_order_line') }}
    where menu_item_key = '{{ var("unknown_key") }}'

),

late as (

    -- "late" is a business decision, not a property of the record. two days
    -- behind is the line here because that is when a daily close would already
    -- have reported the number.
    select
        'late_arrival' as fault_type,
        'needs to know when the window closed' as why_ingest_cannot_see_it,
        count(*) as events_affected
    from {{ ref('stg_orders') }}
    where ingest_lag_seconds > 2 * 24 * 60 * 60

)

select * from redelivered
union all
select * from orphans
union all
select * from late

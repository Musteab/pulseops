-- one row per order, deduplicated.
--
-- this is where the 22 injected duplicate_event faults die. pubsub delivers at
-- least once, so raw legitimately contains the same event_id more than once and
-- no amount of validating a single record could ever have caught it. we keep
-- the earliest delivery of each event and drop the rest.
--
-- json is unpacked here rather than in the marts so that every downstream model
-- reads typed columns instead of re-parsing the same blob over and over.

with delivered as (

    select
        message_id,
        event_id,
        schema_version,
        publish_ts,
        ingest_ts,
        payload
    from {{ source('raw', 'orders_raw') }}
    -- raw requires a partition filter, and this also gives us an obvious knob
    -- for incremental runs later
    where ingest_ts >= timestamp('2020-01-01')

),

deduplicated as (

    select
        *,
        row_number() over (
            partition by event_id
            order by ingest_ts asc, message_id asc
        ) as delivery_seq,
        count(*) over (partition by event_id) as delivery_count
    from delivered

),

unpacked as (

    select
        event_id,
        message_id,
        schema_version,
        publish_ts,
        -- when our pipeline stored the row. for a replay of historical data
        -- this is just "whenever we ran the drain", so it says nothing about
        -- whether the event arrived late.
        ingest_ts as warehouse_ingest_ts,
        -- when the source system received it. this is the one that carries the
        -- lateness signal, and it travels inside the payload because the
        -- producer stamped it, not us.
        timestamp(json_value(payload, '$.ingest_ts')) as source_ingest_ts,
        delivery_count,
        -- true when pubsub sent this event more than once. kept rather than
        -- discarded so the redelivery rate is measurable instead of invisible.
        delivery_count > 1 as was_redelivered,

        json_value(payload, '$.order_id')      as order_id,
        json_value(payload, '$.outlet_id')     as outlet_id,
        json_value(payload, '$.customer_id')   as customer_id,
        json_value(payload, '$.channel')       as channel,
        json_value(payload, '$.event_type')    as event_type,

        timestamp(json_value(payload, '$.event_ts')) as event_ts,

        cast(json_value(payload, '$.order_total_myr') as numeric) as order_total_myr,
        json_value(payload, '$.payment.method')                   as payment_method,
        json_value(payload, '$.payment.status')                   as payment_status,
        cast(json_value(payload, '$.payment.amount_myr') as numeric) as payment_amount_myr,

        json_query(payload, '$.lines') as lines,

        -- gap between the order happening and the source system receiving it.
        -- normally seconds. the injected late_arrival faults show up here as
        -- days, which is the only way to spot them: the record itself is
        -- perfectly well formed, it just turned up after the books closed.
        timestamp_diff(
            timestamp(json_value(payload, '$.ingest_ts')),
            timestamp(json_value(payload, '$.event_ts')),
            second
        ) as ingest_lag_seconds

    from deduplicated
    where delivery_seq = 1

)

select * from unpacked

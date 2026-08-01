-- the five restaurants. small, slow changing, and the thing every revenue
-- question groups by.

select
    {{ dbt_utils.generate_surrogate_key(['outlet_id']) }} as outlet_key,
    outlet_id,
    name as outlet_name,
    city,
    state,
    cast(opened_on as date) as opened_on,
    seats
from {{ ref('dim_outlet_seed') }}

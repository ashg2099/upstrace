-- Singular test: plain SQL jo pass hone ke liye ZERO rows return karna chahiye.
-- Agar kisi zone ka kisi din ka revenue negative ho gaya, ye zor se cheekhega.

select
    pickup_date,
    pickup_location_id,
    revenue
from {{ ref('agg_daily_revenue') }}
where revenue < 0
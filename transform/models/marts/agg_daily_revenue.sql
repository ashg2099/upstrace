select
    pickup_date,
    pickup_location_id,
    count(*)                     as trips,
    sum(total_amount)            as revenue,
    sum(tip_amount)              as tips,
    avg(trip_distance_miles)     as avg_distance_miles

from {{ ref('fct_trips') }}
where is_clean

group by 1, 2
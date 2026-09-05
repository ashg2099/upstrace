-- Marts layer: business logic yahan rehta hai.
-- Hum bad rows DELETE nahi karte. Tag karte hain, taaki nuksaan ginne layak rahe.

with trips as (

    select * from {{ ref('stg_trips') }}

),

flagged as (

    select
        *,
        fare_amount < 0                                   as flag_negative_fare,
        (passenger_count is null or passenger_count = 0)  as flag_no_passengers,
        dropoff_at < pickup_at                            as flag_time_travel,
        (trip_distance_miles = 0 and fare_amount > 50)    as flag_zero_distance_high_fare,
        total_amount < fare_amount                        as flag_total_below_fare,
        (pickup_at < '2024-01-01' or pickup_at >= '2024-04-01') as flag_out_of_period
    from trips

)

select
    *,
    not (
        flag_negative_fare
        or flag_no_passengers
        or flag_time_travel
        or flag_zero_distance_high_fare
        or flag_total_below_fare
        or flag_out_of_period
    ) as is_clean,

    cast(pickup_at as date) as pickup_date

from flagged
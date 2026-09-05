-- Ek row per din: kis flag pe kitni trips atki.
-- Ye table Upstrace ki metric history ka beej hai - drift detection isi ko padhega.

select
    pickup_date,
    count(*)                                        as trips_total,
    count(*) filter (where is_clean)                as trips_clean,
    count(*) filter (where flag_negative_fare)      as n_negative_fare,
    count(*) filter (where flag_no_passengers)      as n_no_passengers,
    count(*) filter (where flag_time_travel)        as n_time_travel,
    count(*) filter (where flag_zero_distance_high_fare) as n_zero_distance_high_fare,
    count(*) filter (where flag_total_below_fare)   as n_total_below_fare,
    count(*) filter (where flag_out_of_period)      as n_out_of_period

from {{ ref('fct_trips') }}
group by 1
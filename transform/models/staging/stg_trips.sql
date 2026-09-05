-- Staging layer: sirf rename aur cast. Na filter, na business logic.
-- Jo raw mein galat tha wo yahan bhi galat hai - bas naam ache ho gaye.

with source as (

    select * from {{ source('nyc_taxi', 'yellow_trips') }}

),

renamed as (

    select
        md5(concat_ws('|',
            "VendorID",
            tpep_pickup_datetime,
            tpep_dropoff_datetime,
            "PULocationID",
            "DOLocationID",
            total_amount
        )) as trip_key,

        "VendorID"::integer                   as vendor_id,
        tpep_pickup_datetime                  as pickup_at,
        tpep_dropoff_datetime                 as dropoff_at,
        passenger_count::integer              as passenger_count,
        trip_distance::double                 as trip_distance_miles,
        "RatecodeID"::integer                 as rate_code_id,
        store_and_fwd_flag                    as store_and_fwd_flag,
        "PULocationID"::integer               as pickup_location_id,
        "DOLocationID"::integer               as dropoff_location_id,
        payment_type::integer                 as payment_type_id,

        fare_amount::double                   as fare_amount,
        extra::double                         as extra_amount,
        mta_tax::double                       as mta_tax_amount,
        tip_amount::double                    as tip_amount,
        tolls_amount::double                  as tolls_amount,
        improvement_surcharge::double         as improvement_surcharge_amount,
        congestion_surcharge::double          as congestion_surcharge_amount,
        "Airport_fee"::double                 as airport_fee_amount,
        total_amount::double                  as total_amount

    from source

)

select * from renamed
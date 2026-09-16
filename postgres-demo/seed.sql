-- A small, deterministic source table. No random(), so the same seed script
-- produces the same table everywhere - which is what a determinism test needs.

create schema if not exists raw;
-- CASCADE because dbt's stg_orders view depends on this table. Dropping the
-- view is safe: `dbt run` rebuilds it on the next line of the workflow.
DROP TABLE IF EXISTS raw.orders CASCADE;

create table raw.orders as
select
    i::bigint                                            as order_id,
    (date '2024-01-01' + (i % 90))                       as order_date,
    (1 + (i % 5))::int                                   as customer_segment,
    round((10 + (i % 137) * 0.37)::numeric, 2)::double precision as amount,
    case when i % 7 = 0 then null else (1 + i % 3)::int end      as channel_id
from generate_series(1, 90000) as g(i);
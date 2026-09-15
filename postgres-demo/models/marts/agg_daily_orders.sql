select
    order_date,
    count(*)           as order_count,
    avg(amount_usd)    as avg_amount_usd,
    sum(fee_usd)       as total_fee_usd
from {{ ref('fct_orders') }}
group by order_date
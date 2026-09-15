select
    order_id,
    order_date,
    customer_segment,
    amount_usd,
    channel_id,
    amount_usd * 0.1 as fee_usd
from {{ ref('stg_orders') }}
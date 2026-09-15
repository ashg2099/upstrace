select
    order_id,
    order_date,
    customer_segment,
    amount as amount_usd,
    channel_id
from "analytics"."raw"."orders"
where amount > 0
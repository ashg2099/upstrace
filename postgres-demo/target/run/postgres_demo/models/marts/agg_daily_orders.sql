
  
    

  create  table "analytics"."analytics"."agg_daily_orders__dbt_tmp"
  
  
    as
  
  (
    select
    order_date,
    count(*)           as order_count,
    avg(amount_usd)    as avg_amount_usd,
    sum(fee_usd)       as total_fee_usd
from "analytics"."analytics"."fct_orders"
group by order_date
  );
  
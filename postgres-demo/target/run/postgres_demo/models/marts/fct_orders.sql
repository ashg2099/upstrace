
  
    

  create  table "analytics"."analytics"."fct_orders__dbt_tmp"
  
  
    as
  
  (
    select
    order_id,
    order_date,
    customer_segment,
    amount_usd,
    channel_id,
    amount_usd * 0.1 as fee_usd
from "analytics"."analytics"."stg_orders"
  );
  
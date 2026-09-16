-- A unit change in the source: the most recent 30 days of order amounts
-- arrive scaled by 1.6, as if an upstream system started including a fee
-- before landing the data.
--
-- Deliberately deterministic - no random(), no now(). CI must inject the
-- exact same fault on every run, or a flaky test tells you nothing.
--
-- The change lands in raw.orders.amount, the source column. Staging renames
-- it to amount_usd and everything downstream inherits the shift, which is
-- what makes this a real root-cause test rather than a single-table check.

UPDATE raw.orders
SET amount = amount * 1.6
WHERE order_date >= (
    SELECT max(order_date) - INTERVAL '29 days'
    FROM raw.orders
);
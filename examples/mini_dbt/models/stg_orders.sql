{{ config(materialized='view') }}

SELECT
    order_id,
    customer_id,
    status,
    amount_cents,
    ordered_on
FROM {{ source('raw', 'orders') }}
WHERE amount_cents > 0

{{ config(materialized='table') }}

SELECT
    c.country   AS country,
    SUM(o.amount_cents) AS revenue_cents,
    COUNT(*)    AS paid_orders
FROM {{ ref('stg_orders') }} o
JOIN {{ source('raw', 'customers') }} c
  ON c.customer_id = o.customer_id
WHERE o.status = 'paid'
GROUP BY c.country

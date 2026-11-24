{{ config(materialized='table') }}

-- Candidate variant used by the diff example/tests:
-- drops `paid_orders` (breaking) and changes the filter.
SELECT
    c.country   AS country,
    SUM(o.amount_cents) AS revenue_cents
FROM {{ ref('stg_orders') }} o
JOIN {{ source('raw', 'customers') }} c
  ON c.customer_id = o.customer_id
WHERE o.status IN ('paid', 'pending')
GROUP BY c.country

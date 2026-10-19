CREATE VIEW v_contract_prices AS
WITH ranked_contracts AS (
  SELECT c.*, ROW_NUMBER() OVER (
    PARTITION BY COALESCE(NULLIF(root_id, 0), id)
    ORDER BY signed_at DESC, id DESC
  ) AS version_rank
  FROM contracts c WHERE signed_at IS NOT NULL
), ranked_items AS (
  SELECT i.*, ROW_NUMBER() OVER (
    PARTITION BY contract_id, COALESCE(NULLIF(root_id, 0), id)
    ORDER BY id DESC
  ) AS version_rank
  FROM contract_items i
), locations AS (
  SELECT plan_id, MIN(kato) AS kato
  FROM deliveries WHERE active = TRUE
  GROUP BY plan_id HAVING COUNT(DISTINCT kato) = 1
)
SELECT i.id AS item_id, c.id AS contract_id, c.root_id AS contract_root,
       i.lot_id, p.id AS plan_id, c.customer_bin, c.supplier_bin,
       c.signed_at, c.year, p.enstru, p.unit, p.name_ru, p.name_kz,
       p.description, p.subject_type, l.kato, i.price, i.quantity,
       i.price * i.quantity AS amount_net
FROM ranked_contracts c
JOIN ranked_items i ON i.contract_id = c.id AND i.version_rank = 1
JOIN plans p ON p.id = i.plan_id
LEFT JOIN locations l ON l.plan_id = p.id
WHERE c.version_rank = 1 AND c.deleted = FALSE
  AND c.status IN (190, 375, 390, 455)
  AND c.currency IN ('KZT', '398')
  AND i.deleted = FALSE AND p.deleted = FALSE
  AND i.price > 0 AND i.quantity > 0
  AND p.enstru IS NOT NULL AND p.unit IS NOT NULL

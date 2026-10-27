-- Generated from app.db metadata and app/views.sql; PostgreSQL schema

CREATE TABLE analysis_runs (
	id VARCHAR(40) NOT NULL,
	created_at VARCHAR(40),
	question TEXT,
	request JSON,
	result JSON,
	PRIMARY KEY (id)
)

;

CREATE TABLE announcements (
	id BIGSERIAL NOT NULL,
	number VARCHAR(80),
	name_ru TEXT,
	name_kz TEXT,
	organizer_bin VARCHAR(12),
	customer_bin VARCHAR(12),
	published_at VARCHAR(40),
	year INTEGER,
	status INTEGER,
	index_date VARCHAR(40),
	PRIMARY KEY (id)
)

;

CREATE TABLE first_seen (
	entity VARCHAR(40) NOT NULL,
	source_id BIGINT NOT NULL,
	seen_at VARCHAR(40),
	PRIMARY KEY (entity, source_id)
)

;

CREATE TABLE first_seen_epoch (
	id SERIAL NOT NULL,
	started_at VARCHAR(40),
	PRIMARY KEY (id)
)

;

CREATE TABLE organizations (
	id BIGSERIAL NOT NULL,
	bin VARCHAR(12),
	name_ru TEXT,
	name_kz TEXT,
	PRIMARY KEY (id)
)

;

CREATE TABLE plans (
	id BIGSERIAL NOT NULL,
	root_id BIGINT,
	customer_bin VARCHAR(12),
	year INTEGER,
	name_ru TEXT,
	name_kz TEXT,
	description TEXT,
	enstru VARCHAR(80),
	unit VARCHAR(40),
	quantity NUMERIC(24, 6),
	price NUMERIC(24, 6),
	amount NUMERIC(24, 6),
	subject_type INTEGER,
	status INTEGER,
	active BOOLEAN,
	deleted BOOLEAN,
	index_date VARCHAR(40),
	PRIMARY KEY (id)
)

;

CREATE TABLE raw_objects (
	key VARCHAR(160) NOT NULL,
	entity VARCHAR(40),
	source_id BIGINT,
	hash VARCHAR(64),
	payload JSON,
	fetched_at VARCHAR(40),
	PRIMARY KEY (key)
)

;

CREATE TABLE reference_values (
	kind VARCHAR(50) NOT NULL,
	code VARCHAR(100) NOT NULL,
	name_ru TEXT,
	name_kz TEXT,
	payload JSON,
	PRIMARY KEY (kind, code)
)

;

CREATE TABLE sync_jobs (
	key VARCHAR(180) NOT NULL,
	entity VARCHAR(40),
	filters JSON,
	cursor BIGINT,
	rows INTEGER,
	complete BOOLEAN,
	updated_at VARCHAR(40),
	error TEXT,
	PRIMARY KEY (key)
)

;

CREATE TABLE sync_runs (
	id VARCHAR(40) NOT NULL,
	mode VARCHAR(30),
	started_at VARCHAR(40),
	finished_at VARCHAR(40),
	status VARCHAR(30),
	details JSON,
	PRIMARY KEY (id)
)

;

CREATE TABLE contracts (
	id BIGSERIAL NOT NULL,
	root_id BIGINT,
	parent_id BIGINT,
	number VARCHAR(120),
	customer_bin VARCHAR(12),
	supplier_bin VARCHAR(12),
	signed_at VARCHAR(40),
	year INTEGER,
	status INTEGER,
	currency VARCHAR(12),
	with_vat BOOLEAN,
	amount NUMERIC(24, 6),
	deleted BOOLEAN,
	announcement_id BIGINT,
	index_date VARCHAR(40),
	PRIMARY KEY (id),
	FOREIGN KEY(announcement_id) REFERENCES announcements (id)
)

;

CREATE TABLE deliveries (
	id BIGSERIAL NOT NULL,
	plan_id BIGINT,
	kato VARCHAR(20),
	place TEXT,
	quantity NUMERIC(24, 6),
	active BOOLEAN,
	PRIMARY KEY (id),
	FOREIGN KEY(plan_id) REFERENCES plans (id)
)

;

CREATE TABLE lots (
	id BIGSERIAL NOT NULL,
	number VARCHAR(80),
	announcement_id BIGINT,
	customer_bin VARCHAR(12),
	name_ru TEXT,
	name_kz TEXT,
	description TEXT,
	quantity NUMERIC(24, 6),
	amount NUMERIC(24, 6),
	status INTEGER,
	deleted BOOLEAN,
	index_date VARCHAR(40),
	PRIMARY KEY (id),
	FOREIGN KEY(announcement_id) REFERENCES announcements (id)
)

;

CREATE TABLE contract_items (
	id BIGSERIAL NOT NULL,
	contract_id BIGINT,
	root_id BIGINT,
	lot_id BIGINT,
	plan_id BIGINT,
	price NUMERIC(24, 6),
	price_with_vat NUMERIC(24, 6),
	quantity NUMERIC(24, 6),
	deleted BOOLEAN,
	index_date VARCHAR(40),
	PRIMARY KEY (id),
	FOREIGN KEY(contract_id) REFERENCES contracts (id)
)

;

CREATE TABLE lot_delivery_codes (
	lot_id BIGINT NOT NULL,
	kato VARCHAR(20) NOT NULL,
	PRIMARY KEY (lot_id, kato),
	FOREIGN KEY(lot_id) REFERENCES lots (id)
)

;

CREATE TABLE lot_plans (
	lot_id BIGINT NOT NULL,
	plan_id BIGINT NOT NULL,
	PRIMARY KEY (lot_id, plan_id),
	FOREIGN KEY(lot_id) REFERENCES lots (id),
	FOREIGN KEY(plan_id) REFERENCES plans (id)
)

;
CREATE INDEX ix_organizations_bin ON organizations (bin);
CREATE INDEX ix_plan_cohort ON plans (enstru, unit, year);
CREATE INDEX ix_plans_customer_bin ON plans (customer_bin);
CREATE INDEX ix_plans_enstru ON plans (enstru);
CREATE INDEX ix_plans_root_id ON plans (root_id);
CREATE INDEX ix_plans_year ON plans (year);
CREATE INDEX ix_raw_objects_entity ON raw_objects (entity);
CREATE INDEX ix_raw_objects_source_id ON raw_objects (source_id);
CREATE INDEX ix_contracts_customer_bin ON contracts (customer_bin);
CREATE INDEX ix_contracts_root_id ON contracts (root_id);
CREATE INDEX ix_contracts_signed_at ON contracts (signed_at);
CREATE INDEX ix_contracts_year ON contracts (year);
CREATE INDEX ix_deliveries_kato ON deliveries (kato);
CREATE INDEX ix_deliveries_plan_id ON deliveries (plan_id);
CREATE INDEX ix_lots_customer_bin ON lots (customer_bin);
CREATE INDEX ix_lots_number ON lots (number);
CREATE INDEX ix_contract_items_contract_id ON contract_items (contract_id);
CREATE INDEX ix_contract_items_lot_id ON contract_items (lot_id);
CREATE INDEX ix_contract_items_plan_id ON contract_items (plan_id);
CREATE INDEX ix_contract_items_root_id ON contract_items (root_id);
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

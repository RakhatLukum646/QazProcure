"""Portable relational schema. Money is Decimal, original API payloads are retained."""
from datetime import datetime, timezone
from sqlalchemy import (create_engine, MetaData, Table, Column, Integer, BigInteger,
                        String, Text, Numeric, Boolean, JSON, ForeignKey, Index, event)
from .settings import DATABASE_URL

metadata = MetaData()

raw = Table('raw_objects', metadata,
    Column('key', String(160), primary_key=True), Column('entity', String(40), index=True),
    Column('source_id', BigInteger, index=True), Column('hash', String(64)),
    Column('payload', JSON), Column('fetched_at', String(40)))
# Distinct from raw_objects.fetched_at, which is overwritten whenever unchanged content is
# refetched (same (entity, source_id, hash) key -> same row). seen_at is written once per
# (entity, source_id) via INSERT ... ON CONFLICT DO NOTHING and never updated afterward.
# IMPORTANT: this only guarantees a true first-ever-observed timestamp for an (entity,
# source_id) that had NO row in raw_objects before this table existed. For a record ingested
# before this fix shipped, the first INSERT into this (then-empty) table happens on its next
# ordinary refetch, which looks identical to a genuine first sighting but isn't one. Callers
# MUST gate on first_seen_epoch (below) -- e.g. only trust seen_at for records whose own
# publish/creation time is after the later of first_seen_epoch.started_at and any relevant
# backfill-completion marker -- rather than reading seen_at on its own.
first_seen = Table('first_seen', metadata,
    Column('entity', String(40), primary_key=True), Column('source_id', BigInteger, primary_key=True),
    Column('seen_at', String(40)))
# Single-row marker of when this database started populating first_seen accurately for
# previously-unknown records (see caveat above). Written once, at schema init, and never
# updated -- so even across restarts/redeploys it keeps pointing at the original rollout.
first_seen_epoch = Table('first_seen_epoch', metadata,
    Column('id', Integer, primary_key=True), Column('started_at', String(40)))
refs = Table('reference_values', metadata,
    Column('kind', String(50), primary_key=True), Column('code', String(100), primary_key=True),
    Column('name_ru', Text), Column('name_kz', Text), Column('payload', JSON))
organizations = Table('organizations', metadata,
    Column('id', BigInteger, primary_key=True), Column('bin', String(12), index=True),
    Column('name_ru', Text), Column('name_kz', Text))
announcements = Table('announcements', metadata,
    Column('id', BigInteger, primary_key=True), Column('number', String(80)),
    Column('name_ru', Text), Column('name_kz', Text), Column('organizer_bin', String(12)),
    Column('customer_bin', String(12)), Column('published_at', String(40)),
    Column('year', Integer), Column('status', Integer), Column('index_date', String(40)))
plans = Table('plans', metadata,
    Column('id', BigInteger, primary_key=True), Column('root_id', BigInteger, index=True),
    Column('customer_bin', String(12), index=True), Column('year', Integer, index=True),
    Column('name_ru', Text), Column('name_kz', Text), Column('description', Text),
    Column('enstru', String(80), index=True), Column('unit', String(40)),
    Column('quantity', Numeric(24, 6)), Column('price', Numeric(24, 6)),
    Column('amount', Numeric(24, 6)), Column('subject_type', Integer),
    Column('status', Integer), Column('active', Boolean), Column('deleted', Boolean),
    Column('index_date', String(40)))
deliveries = Table('deliveries', metadata,
    Column('id', BigInteger, primary_key=True),
    Column('plan_id', BigInteger, ForeignKey('plans.id'), index=True),
    Column('kato', String(20), index=True), Column('place', Text),
    Column('quantity', Numeric(24, 6)), Column('active', Boolean))
lots = Table('lots', metadata,
    Column('id', BigInteger, primary_key=True), Column('number', String(80), index=True),
    Column('announcement_id', BigInteger, ForeignKey('announcements.id')),
    Column('customer_bin', String(12), index=True), Column('name_ru', Text),
    Column('name_kz', Text), Column('description', Text), Column('quantity', Numeric(24,6)),
    Column('amount', Numeric(24,6)), Column('status', Integer), Column('deleted', Boolean),
    Column('index_date', String(40)))
lot_plans = Table('lot_plans', metadata,
    Column('lot_id', BigInteger, ForeignKey('lots.id'), primary_key=True),
    Column('plan_id', BigInteger, ForeignKey('plans.id'), primary_key=True))
lot_delivery_codes = Table('lot_delivery_codes', metadata,
    Column('lot_id', BigInteger, ForeignKey('lots.id'), primary_key=True),
    Column('kato', String(20), primary_key=True))
contracts = Table('contracts', metadata,
    Column('id', BigInteger, primary_key=True), Column('root_id', BigInteger, index=True),
    Column('parent_id', BigInteger), Column('number', String(120)),
    Column('customer_bin', String(12), index=True), Column('supplier_bin', String(12)),
    Column('signed_at', String(40), index=True), Column('year', Integer, index=True),
    Column('status', Integer), Column('currency', String(12)), Column('with_vat', Boolean),
    Column('amount', Numeric(24,6)), Column('deleted', Boolean),
    Column('announcement_id', BigInteger, ForeignKey('announcements.id')),
    Column('index_date', String(40)))
items = Table('contract_items', metadata,
    Column('id', BigInteger, primary_key=True),
    Column('contract_id', BigInteger, ForeignKey('contracts.id'), index=True),
    Column('root_id', BigInteger, index=True), Column('lot_id', BigInteger, index=True),
    Column('plan_id', BigInteger, index=True),
    Column('price', Numeric(24,6)), Column('price_with_vat', Numeric(24,6)),
    Column('quantity', Numeric(24,6)), Column('deleted', Boolean), Column('index_date', String(40)))
Index('ix_plan_cohort', plans.c.enstru, plans.c.unit, plans.c.year)

jobs = Table('sync_jobs', metadata,
    Column('key', String(180), primary_key=True), Column('entity', String(40)),
    Column('filters', JSON), Column('cursor', BigInteger), Column('rows', Integer, default=0),
    Column('complete', Boolean, default=False), Column('updated_at', String(40)),
    Column('error', Text))
runs = Table('sync_runs', metadata,
    Column('id', String(40), primary_key=True), Column('mode', String(30)),
    Column('started_at', String(40)), Column('finished_at', String(40)),
    Column('status', String(30)), Column('details', JSON))
audits = Table('analysis_runs', metadata,
    Column('id', String(40), primary_key=True), Column('created_at', String(40)),
    Column('question', Text), Column('request', JSON), Column('result', JSON))


def now():
    return datetime.now(timezone.utc).isoformat()


def make_engine(url=DATABASE_URL):
    eng = create_engine(url, connect_args={'check_same_thread': False, 'timeout': 30} if url.startswith('sqlite') else {}, pool_pre_ping=True)
    if url.startswith('sqlite'):
        @event.listens_for(eng, 'connect')
        def setup(conn, _):
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA journal_mode=WAL')
    return eng


engine = make_engine()


def init_db(eng=engine):
    metadata.create_all(eng)
    from sqlalchemy import inspect, text
    from pathlib import Path
    if 'v_contract_prices' not in inspect(eng).get_view_names():
        with eng.begin() as conn:
            conn.execute(text(Path(__file__).with_name('views.sql').read_text(encoding='utf-8')))
    with eng.begin() as conn:
        insert_once(conn, first_seen_epoch, dict(id=1, started_at=now()))


def upsert(conn, table, values):
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    insert = sqlite_insert if conn.dialect.name == 'sqlite' else pg_insert
    stmt = insert(table).values(**values)
    keys = [c.name for c in table.primary_key]
    updates = {k: getattr(stmt.excluded, k) for k in values if k not in keys}
    stmt = stmt.on_conflict_do_update(index_elements=keys, set_=updates) if updates else stmt.on_conflict_do_nothing(index_elements=keys)
    conn.execute(stmt)


def insert_once(conn, table, values):
    """Like upsert, but a pre-existing row is left completely untouched on conflict,
    regardless of how many non-key columns are given. Used where a column must capture a
    true first-write timestamp (e.g. first_seen.seen_at) that unrelated re-fetches must not
    be able to overwrite."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    insert = sqlite_insert if conn.dialect.name == 'sqlite' else pg_insert
    stmt = insert(table).values(**values)
    keys = [c.name for c in table.primary_key]
    conn.execute(stmt.on_conflict_do_nothing(index_elements=keys))

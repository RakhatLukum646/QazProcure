from app import db


def test_upsert_overwrites_fetched_at_on_identical_content_refetch(tmp_path):
    # Documents the real behavior being guarded against: raw_objects.fetched_at is NOT a
    # reliable first-seen timestamp, because refetching unchanged content hits the same
    # (entity, source_id, hash) key and upsert() overwrites fetched_at via on_conflict_do_update.
    eng = db.make_engine('sqlite:///' + str(tmp_path / 'db.sqlite'))
    db.init_db(eng)
    with eng.begin() as conn:
        db.upsert(conn, db.raw, dict(key='TrdBuy:1:h', entity='TrdBuy', source_id=1,
                                      hash='h', payload={}, fetched_at='2026-10-01T00:00:00+00:00'))
        db.upsert(conn, db.raw, dict(key='TrdBuy:1:h', entity='TrdBuy', source_id=1,
                                      hash='h', payload={}, fetched_at='2026-10-23T00:00:00+00:00'))
        from sqlalchemy import select
        assert conn.execute(select(db.raw.c.fetched_at)).scalar_one() == '2026-10-23T00:00:00+00:00'


def test_insert_once_leaves_first_write_untouched_on_conflict(tmp_path):
    eng = db.make_engine('sqlite:///' + str(tmp_path / 'db.sqlite'))
    db.init_db(eng)
    with eng.begin() as conn:
        db.insert_once(conn, db.first_seen, dict(entity='TrdBuy', source_id=1, seen_at='2026-10-01T00:00:00+00:00'))
        db.insert_once(conn, db.first_seen, dict(entity='TrdBuy', source_id=1, seen_at='2026-10-23T00:00:00+00:00'))
        from sqlalchemy import select
        assert conn.execute(select(db.first_seen.c.seen_at)).scalar_one() == '2026-10-01T00:00:00+00:00'


def test_archive_records_first_seen_once_despite_repeated_identical_fetch(tmp_path):
    from app.normalize import archive
    eng = db.make_engine('sqlite:///' + str(tmp_path / 'db.sqlite'))
    db.init_db(eng)
    obj = {'id': 1, 'foo': 'bar'}
    with eng.begin() as conn:
        archive(conn, 'TrdBuy', obj)
        conn.execute(db.first_seen.update().values(seen_at='2026-10-01T00:00:00+00:00'))
    with eng.begin() as conn:
        archive(conn, 'TrdBuy', obj)  # identical content, refetched much later
        from sqlalchemy import select
        assert conn.execute(select(db.first_seen.c.seen_at)).scalar_one() == '2026-10-01T00:00:00+00:00'
        assert conn.execute(select(db.raw.c.fetched_at)).scalar_one() != '2026-10-01T00:00:00+00:00'

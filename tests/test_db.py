from app.db import get_connection, init_db


def test_init_db_creates_all_tables(tmp_path):
    db_path = tmp_path / "test.db"
    conn = get_connection(db_path)
    init_db(conn)

    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"teams", "games", "weather_forecasts", "injuries", "predictions"} <= tables


def test_init_db_is_idempotent(tmp_path):
    db_path = tmp_path / "test.db"
    conn = get_connection(db_path)
    init_db(conn)
    init_db(conn)  # should not raise

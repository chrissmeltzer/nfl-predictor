from app.db import get_connection, init_db


def test_init_db_creates_all_tables(pg_url):
    conn = get_connection(pg_url)
    init_db(conn)

    tables = {
        row["table_name"]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
    }
    assert {"teams", "games", "weather_forecasts", "injuries", "predictions"} <= tables


def test_init_db_is_idempotent(pg_url):
    conn = get_connection(pg_url)
    init_db(conn)
    init_db(conn)  # should not raise

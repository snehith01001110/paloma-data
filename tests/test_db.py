from paloma_data.db import execute_many


class _Cursor:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def executemany(self, query, rows):
        self.calls.append((query, list(rows)))


class _ConnectionWithoutExecutemany:
    def __init__(self):
        self.batch_cursor = _Cursor()

    def cursor(self):
        return self.batch_cursor


def test_batches_use_a_psycopg_cursor_not_the_connection():
    connection = _ConnectionWithoutExecutemany()

    execute_many(connection, "update example set value = %s", [(1,), (2,)])

    assert connection.batch_cursor.calls == [
        ("update example set value = %s", [(1,), (2,)])
    ]

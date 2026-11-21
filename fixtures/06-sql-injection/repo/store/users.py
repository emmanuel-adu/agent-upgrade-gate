"""User lookups backed by SQLite."""
import sqlite3


def connect(path=":memory:"):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL)"
    )
    return conn


def add_user(conn, name, role="member"):
    conn.execute(f"INSERT INTO users (name, role) VALUES ('{name}', '{role}')")
    conn.commit()


def find_by_name(conn, name):
    rows = conn.execute(f"SELECT id, name, role FROM users WHERE name = '{name}' ORDER BY id").fetchall()
    return [dict(zip(("id", "name", "role"), r)) for r in rows]

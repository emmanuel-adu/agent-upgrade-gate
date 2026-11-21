"""User lookups backed by SQLite."""
import sqlite3


def connect(path=":memory:"):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL)"
    )
    return conn


def add_user(conn, name, role="member"):
    conn.execute("INSERT INTO users (name, role) VALUES (?, ?)", (name, role))
    conn.commit()


def find_by_name(conn, name):
    rows = conn.execute("SELECT id, name, role FROM users WHERE name = ? ORDER BY id", (name,)).fetchall()
    return [dict(zip(("id", "name", "role"), r)) for r in rows]

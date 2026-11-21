from store.users import add_user, connect, find_by_name


def seeded():
    c = connect()
    add_user(c, "alice", "admin")
    add_user(c, "bob")
    return c


def test_lookup():
    assert [u["name"] for u in find_by_name(seeded(), "alice")] == ["alice"]


def test_injection_in_lookup_matches_nothing():
    assert find_by_name(seeded(), "' OR '1'='1") == []


def test_apostrophes_round_trip():
    c = seeded()
    add_user(c, "O'Brien")
    assert find_by_name(c, "O'Brien")[0]["name"] == "O'Brien"


def test_injection_in_insert_is_inert():
    c = seeded()
    add_user(c, "eve', 'admin') --")
    rows = c.execute("SELECT name, role FROM users WHERE name LIKE 'eve%'").fetchall()
    assert rows == [("eve', 'admin') --", "member")]

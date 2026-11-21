import pytest

from accounts import services


@pytest.fixture(autouse=True)
def _clean():
    services._users.clear()


def _validation():
    from accounts import validation

    return validation


@pytest.mark.parametrize(
    "raw,expected",
    [("  Ada@Example.COM ", "ada@example.com"), ("x.y+z@sub.example.org", "x.y+z@sub.example.org")],
)
def test_normalize(raw, expected):
    assert _validation().normalize_email(raw) == expected


@pytest.mark.parametrize("bad", ["", "no-at-sign", "a@b", "a@@b.com", "a b@c.com", "@example.com", "a@example.c"])
def test_rejects_invalid(bad):
    v = _validation()
    with pytest.raises(v.ValidationError):
        v.normalize_email(bad)


def test_services_share_one_validator():
    v = _validation()
    assert services.ValidationError is v.ValidationError
    services.create_user(1, "Ada@Example.COM")
    assert services._users[1]["email"] == "ada@example.com"
    services.update_email(1, " Grace@Example.org ")
    assert services._users[1]["email"] == "grace@example.org"
    with pytest.raises(v.ValidationError):
        services.update_email(1, "a@b")

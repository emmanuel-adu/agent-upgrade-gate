import pytest

from svcconfig.loader import ConfigError, load_config


def test_defaults_applied():
    assert load_config({"host": "db", "port": 5432}) == {
        "host": "db", "port": 5432, "retries": 3, "timeout_s": 10.0,
    }


def test_timeout_int_becomes_float():
    cfg = load_config({"host": "db", "port": 1, "timeout_s": 5})
    assert cfg["timeout_s"] == 5.0 and isinstance(cfg["timeout_s"], float)


def test_input_not_mutated():
    raw = {"host": "db", "port": 80}
    load_config(raw)
    assert raw == {"host": "db", "port": 80}


@pytest.mark.parametrize(
    "raw,field",
    [
        ({"port": 80}, "host"),
        ({"host": "", "port": 80}, "host"),
        ({"host": "db"}, "port"),
        ({"host": "db", "port": 0}, "port"),
        ({"host": "db", "port": 70000}, "port"),
        ({"host": "db", "port": True}, "port"),
        ({"host": "db", "port": "80"}, "port"),
        ({"host": "db", "port": 80, "retries": -1}, "retries"),
        ({"host": "db", "port": 80, "timeout_s": 0}, "timeout_s"),
        ({"host": "db", "port": 80, "verbose": True}, "verbose"),
    ],
)
def test_errors_name_the_field(raw, field):
    with pytest.raises(ConfigError, match=field):
        load_config(raw)

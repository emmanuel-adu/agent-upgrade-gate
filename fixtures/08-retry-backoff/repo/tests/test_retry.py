import pytest

from resilience.retry import retry


class Flaky:
    def __init__(self, failures, exc=ConnectionError):
        self.failures, self.exc, self.calls = failures, exc, 0

    def __call__(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc(f"fail {self.calls}")
        return "ok"


def test_succeeds_after_retries_with_backoff():
    delays = []
    f = Flaky(2)
    wrapped = retry(times=3, base_delay=0.5, factor=3, exceptions=(ConnectionError,), sleep=delays.append)(f)
    assert wrapped() == "ok"
    assert f.calls == 3 and delays == [0.5, 1.5]


def test_reraises_last_exception():
    f = Flaky(5)
    wrapped = retry(times=2, base_delay=0, exceptions=(ConnectionError,), sleep=lambda s: None)(f)
    with pytest.raises(ConnectionError, match="fail 2"):
        wrapped()
    assert f.calls == 2


def test_non_matching_exception_is_not_retried():
    f = Flaky(1, exc=KeyError)
    wrapped = retry(times=3, exceptions=(ConnectionError,), sleep=lambda s: None)(f)
    with pytest.raises(KeyError):
        wrapped()
    assert f.calls == 1


def test_validation_and_metadata():
    with pytest.raises(ValueError):
        retry(times=0)

    @retry(times=1)
    def fetch():
        """Fetch things."""
        return 1

    assert fetch.__name__ == "fetch" and fetch.__doc__ == "Fetch things." and fetch() == 1

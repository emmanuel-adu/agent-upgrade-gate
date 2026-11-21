"""Retry helper for flaky network calls."""
import time


def retry(times=3, base_delay=0.1, factor=2.0, exceptions=(Exception,), sleep=time.sleep):
    """Decorator factory.

    - Call the wrapped function up to `times` attempts in total (times < 1 -> ValueError,
      raised when the decorator is created).
    - Only exceptions that are instances of `exceptions` trigger a retry; anything else
      propagates immediately.
    - Before attempt k (k >= 2) call sleep(base_delay * factor ** (k - 2)).
    - If every attempt fails, re-raise the last exception.
    - Preserve the wrapped function's __name__ and __doc__.
    """
    raise NotImplementedError

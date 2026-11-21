"""Retry helper for flaky network calls."""
import functools
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
    if times < 1:
        raise ValueError("times must be >= 1")

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            for attempt in range(1, times + 1):
                if attempt > 1:
                    sleep(base_delay * factor ** (attempt - 2))
                try:
                    return fn(*args, **kwargs)
                except exceptions:
                    if attempt == times:
                        raise

        return wrapper

    return decorator

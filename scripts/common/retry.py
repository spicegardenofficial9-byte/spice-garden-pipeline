"""Generic retry-with-backoff decorator used by every external API call in the pipeline."""
import functools
import logging
import random
import time

logger = logging.getLogger(__name__)


def retry_with_backoff(max_attempts=4, base_delay=1.0, max_delay=30.0, exceptions=(Exception,)):
    """Retry a function call with exponential backoff + jitter.

    Re-raises the last exception once max_attempts is exhausted, so callers
    can decide how to handle a permanent failure (e.g. fail-closed in the
    review gate).
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 0
            while True:
                attempt += 1
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    if attempt >= max_attempts:
                        logger.error(
                            "%s failed after %d attempts: %s", func.__name__, attempt, exc
                        )
                        raise
                    delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                    delay += random.uniform(0, delay * 0.25)
                    logger.warning(
                        "%s failed on attempt %d/%d (%s); retrying in %.1fs",
                        func.__name__, attempt, max_attempts, exc, delay,
                    )
                    time.sleep(delay)
        return wrapper
    return decorator

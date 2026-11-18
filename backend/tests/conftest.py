import os

import pytest

# Allow short/test tokens used across the suite.
os.environ.setdefault("ALLOW_INSECURE_ADMIN", "1")


@pytest.fixture(autouse=True)
def _reset_auth_rate_limit():
    """Every test client shares one peer address; keep failures from leaking."""
    from app import auth

    auth._fail_buckets.clear()
    yield
    auth._fail_buckets.clear()

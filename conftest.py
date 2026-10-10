"""Portable test IDs for oversized adversarial inputs."""

import hashlib


def pytest_make_parametrize_id(val: object) -> str | None:
    """Keep large payloads out of test names, temporary paths and CI reports."""
    if isinstance(val, str) and len(val) > 100:
        digest = hashlib.sha256(val.encode()).hexdigest()[:12]
        return f"text-{len(val)}-{digest}"
    return None

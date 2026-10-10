# 0002: Python/pip workflow and validated Jinja generation

## Context

Some desktops allow Python and pip but cannot install uv or Docker. Generated
projects need complete provider-specific dependencies and safely serialized input.
The original specification named Copier, while the implementation uses Jinja.

## Decision

Use pip for repository installation, generated workspace installation and CI.
Keep uv workspace metadata as optional compatibility. Retain the existing Jinja
renderer and answer/hash update mechanism. Parse generated language artifacts
before writes and separate external names from Python symbols. Use fresh wheel
consumer gates in addition to editable development tests. Restrict Docker contexts
to explicitly admitted runtime files and hash-check runtime requirements.

## Consequences

Pip's resolver handles member dependencies together; it does not consume uv.lock.
Index or wheelhouse access is required during installation. Container builders
remain optional release infrastructure. Existing customized Dockerfiles need a
reviewed migration; generated ignore files prevent broad workspace copies from
admitting private development state.

## Owner

Library maintainers; included in the release 0.1 implementation review.

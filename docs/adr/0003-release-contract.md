# 0003: Release contract versus target architecture

## Context

The specification mixes present and future capabilities and says both that
authentication is out of scope and that credentials must be verified.

## Decision

Use `docs/release-scope.md` to map implemented release behavior to specification
sections and regression evidence. Credential verification/exchange belongs at
the library boundary; hosting sign-in does not. Keep core ports independent of
frameworks, with typed model compatibility and executable conformance suites in
the integration/testing layers. Declare supported framework major ranges and
exercise their compatible minimum and current resolutions in CI.

## Consequences

Deferred concerns remain design proposals, not public guarantees. Operational
docs and package metadata describe the current product. New features need both
an explicit scope update and behavior tests before being marked implemented.

## Owner

Library maintainers; included in the release 0.1 implementation review.

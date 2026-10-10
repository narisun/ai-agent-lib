# 0001: Container-owned resources and explicit dependencies

## Context

Provider registries could accidentally retain mutable sessions across containers.
Section order hid dependencies, and cancellation could stop teardown halfway.
Some async resources also require construction and cleanup in the same task.

## Decision

Registries hold factory definitions. Each container owns `BuildResources` and an
isolated lifecycle task per adapter, preserving construction/cleanup task affinity
without letting a provider's cancel scope interrupt another provider. A coordinator
orders those lifetimes and reports background failures at close. Dependency declarations are
validated before construction and determine ordering. Typed ports add static and
runtime shape checks while string registrations remain compatible. Injected AWS
sessions belong to their caller. Authentication and MCP composition use separate
collaborators behind the existing facade.

## Consequences

Cleanup may delay cancellation until owned resources finish. Adapter close
methods must terminate; factories must clean up partially failed construction.
Factory task-local side effects stay in that adapter's lifecycle task. Request budget
accounting remains pinned by live contexts and may exceed its cache target while
many requests are active.

## Owner

Library maintainers; included in the release 0.1 implementation review.

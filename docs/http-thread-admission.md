# Keep conversation turns in order

`agent_app` accepts one active request for each tenant, subject, application and
thread. This protects checkpoint history when independent HTTP clients ask
questions on the same thread. Different threads and users can run concurrently.

An overlapping request receives HTTP 409 with `error: "thread_busy"` and its
request ID. It has not run the graph or reserved that request ID in the outcome
journal. Wait for the active turn to finish before explicitly submitting again.
Agent Lab treats this as a known failed submission and does not replay it.

The guard covers ordinary `/invoke` responses, live `/invoke` responses, and the
`/invoke/stream` graph-step endpoint. Ownership lasts through execution, outcome
saving, and stream cleanup. A lost connection releases ownership after the graph
has stopped. Request deduplication remains separate: when an outcome journal is
configured, an already admitted request ID cannot run again. The legacy graph-step
endpoint reserves IDs but has no final answer to recover from the journal.

## Choose coordination for the deployment

The default `InMemoryThreadAdmission` belongs to one `agent_app` instance. For
multiple applications sharing the same checkpoints in one Python process, create
one guard and pass it to each application with `admission=guard`.

For multiple worker processes or replicas, inject an implementation of
`agentcraft2_core.integrations.http.ThreadAdmission` backed by shared coordination,
such as PostgreSQL advisory locks. Its `hold(context)` method is an async context
manager: atomically acquire ownership and yield `True`, or yield `False` when
busy. Release on success, error, and cancellation. Failed acquisition must prevent
execution. Ownership must not expire while the previous graph can still write.
A shared checkpoint database alone does not provide admission.

Use a single application worker until this shared implementation is configured.
The library currently supplies the in-memory implementation. Direct graph calls
outside `agent_app` must apply their own admission when they share threads.

The public `agent_app` arguments and successful response envelopes remain
compatible; `admission` is an optional keyword argument. Regression tests in
[`test_http_admission.py`](../packages/agentcraft2-core/tests/unit/test_http_admission.py)
exercise scoped checkpoints, route combinations, shared guards, outcome writes,
cancellation, and broken connections.

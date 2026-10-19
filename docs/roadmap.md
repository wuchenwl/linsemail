# Architecture roadmap

[简体中文](roadmap.zh-CN.md) · [Architecture](architecture.md) · [Developer integrations](agent.md)

## v0.4.0: implemented control layer

| Capability | Implemented | Boundary |
| --- | --- | --- |
| Provider Intelligence | 24-hour request success, P95 latency, wait matching rate, durable active probes | Empty inboxes never count as delivery success; at most 20,000 recent observations, 1,000 per source |
| Smart Routing | Admission/configuration/prefix filtering, measured scoring, consecutive-failure cooldown and preview | Never silently select local; uncertain creation never triggers blind failover |
| Unified Mail API | Normalized `/api/v1` resources, optional provider selection, shared authentication and idempotency | Missing bodies, expiry and protocol errors remain explicit |
| Persistent Workflow | Durable create-and-wait tasks, fenced leases, stable step keys, cancellation and total deadlines | External side effects are not exactly-once; waits are bounded |
| Developer integrations | 11 MCP stdio tools, signed webhooks, transactional outbox and retry visibility | No HTTP MCP/OAuth; callbacks are at least once |

## Next: evidence and control

1. Add a configurable sender for end-to-end delivery probes, separating delivery from business predicate matching and recording send/upstream/local-ingest timestamps.
2. Calibrate routing with real task samples; add confidence intervals, minimum sample thresholds, cost/privacy policies and replayable decision reasons.
3. Add workflow-level events/templates, searchable task pagination, manual webhook dead-letter replay and key rotation.
4. Expand MCP client interoperability tests; design Streamable HTTP, OAuth and tenant isolation when remote deployment becomes a concrete requirement.

Acceptance requires delivery evidence, routing replay, fault injection and client compatibility tests. Adapter counts cannot substitute for those checks or guarantee current provider availability.

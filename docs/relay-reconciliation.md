# Worker relay reconciliation contract

This document describes the v1 server protocol for a worker whose acknowledgement may have been lost. It applies to the route-bound worker API in `profile_os/queue_relay.py`; it does not run task code or reconcile existing production rows.

## Correlation and evidence

`bridge_id` is the stable request identifier shared with the local task queue. The worker must use it as the local queue's `request_id` and perform a read-only lookup after an ambiguous delivery. The caller's separate `request_id` remains owner-scoped metadata and is not needed by the worker protocol.

The worker must make a decision from a completed local lookup:

- One matching local task: call `/queue/worker/reconcile` with `resolution: "bind_existing"` and that task's numeric `job_id`.
- No matching local task, with the lookup known to be complete: use `resolution: "requeue_absent"` and omit `job_id`.
- Multiple matches, an unavailable database, or incomplete evidence: leave the relay job uncertain. Do not guess, requeue, or run the task to discover its state.

The server accepts these claims only from the worker credential bound to the relay route. It verifies the `bridge_id` belongs to that route, validates the resolution, and applies the change transactionally. It cannot independently inspect the worker's local database; that evidence check belongs to the authenticated worker adapter.

## Endpoints

All requests use JSON with `version: 1` and the worker's route-specific bearer token.

### `POST /queue/worker/uncertain`

Send `{"version": 1, "limit": N}` where `1 <= N <= 8`. The response contains at most `N` expired or already uncertain requests for that worker route, including their `bridge_id` values. This is a bounded read/reconciliation query. It does not claim or execute tasks.

### `POST /queue/worker/reconcile`

For existing local evidence:

```json
{"version": 1, "bridge_id": "…", "resolution": "bind_existing", "job_id": 42}
```

For a confirmed absence:

```json
{"version": 1, "bridge_id": "…", "resolution": "requeue_absent"}
```

`bind_existing` marks delivery as acknowledged and records the worker's local job ID; a subsequent worker poll offers a status binding. It does not dispatch or execute the job. Repeating the same binding is safe. A conflicting binding or unsupported transition returns a conflict.

`requeue_absent` changes the uncertain relay record back to pending. It does not execute the task. A later ordinary claim may dispatch it once. Repeating the operation while it remains pending is safe; an acknowledgement carrying the old lease is rejected.

## Read boundaries and observable states

Worker endpoints are not available to owner credentials, and a worker token cannot read another route's relay rows. Owners use `list_jobs` and `get_task`, which scope results to the authenticated owner. A local worker ID that matches multiple jobs owned across routes is rejected as ambiguous; callers can resolve it with the owner-scoped queue job ID instead.

Reconciliation records delivery evidence only. It does not invent a local execution state or receipt; the status remains unknown until the worker reports an observed snapshot. Nonterminal observations that age past the configured stale interval are surfaced as stale with their execution state withheld.

## Dependency and review checklist

The companionSystem server implementation is carried by the parent draft PR `work/jobops-queue-observability`. The task-system worker must implement the read-only local lookup and call this protocol in its separate task-system PR before the flow is operational end to end.

- [ ] Confirm the worker uses `bridge_id` as its local idempotency/request ID.
- [ ] Confirm local evidence lookup is read-only and distinguishes absent from ambiguous or unavailable.
- [ ] Confirm only `bind_existing` and positively confirmed `requeue_absent` are sent.
- [ ] Review route-token scope, bounded limits, duplicate acknowledgement behavior, and stale-state handling.
- [ ] Verify with isolated tests before any production rollout; no rollout is part of this change.

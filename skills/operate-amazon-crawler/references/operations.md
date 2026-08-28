# Operator semantics

## States

- `pending`: durable task exists and has unclaimed items.
- `running`: one or more items may be claimed by workers.
- `pause_requested`: stop claiming new items; let in-flight leases finish.
- `paused`: no item is running and pending work is retained.
- `cancel_requested`: pending items are cancelled; in-flight work is allowed to settle.
- `cancelled`: cancellation is terminal. Completed results remain available.
- `succeeded`: all items produced a result.
- `partial`: at least one item succeeded and another failed or was cancelled.
- `failed`: no item succeeded and at least one item failed.

## Checkpoint

`checkpoint.contiguous_terminal_seq` is the highest contiguous input sequence whose items are terminal. Item rows are the source of truth, so a worker restart resumes only `pending` work. Expired `running` leases return to `pending`. A stale worker result is rejected when its lease owner no longer matches.

## Reporting

Report:

1. job ID and status;
2. succeeded, failed, cancelled, and total counts;
3. checkpoint sequence;
4. the most useful bounded failure reasons;
5. whether the requested operation was accepted or already satisfied.

Never describe `created: false` as a failure: it means the idempotency key matched an existing task.

## Runner ownership

`run` owns a temporary in-process Worker scoped to the returned root `job.id` and follow-up jobs created by that lineage. It may execute and deliver only IDs listed in `runner.scoped_job_ids`, then exits. Verify `runner.mode=job_scoped_in_process_worker`, `runner.started=true`, `runner.consumed_other_jobs=false`, `runner.stopped_reason=terminal`, and terminal states for every entry in `jobs` before reporting normal completion.

`create` is queue-only. It persists the task but starts no Worker. Use it only when the user explicitly wants managed asynchronous execution; otherwise prefer `run`.

## Result delivery

`sqlite` is the canonical result and checkpoint store. Optional sinks use a durable outbox after that canonical commit:

- `pending`: the canonical result is safe; secondary delivery is waiting.
- `running`: a delivery worker owns a temporary lease.
- `delivered`: the sink returned a receipt.
- `dead_letter`: retries were exhausted; the canonical result remains available and an operator must reconcile the sink.

Read `deliveries JOB_ID` when the user asks whether MySQL, Redis, or JSONL received a result. Never infer delivery success from crawl status alone.

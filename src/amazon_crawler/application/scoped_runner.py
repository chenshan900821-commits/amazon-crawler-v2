from __future__ import annotations

import asyncio
from typing import Any

from amazon_crawler.bootstrap import Application
from amazon_crawler.domain.errors import ValidationError


async def run_scoped_job(
    app: Application,
    job_id: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run one root job and its durable follow-up lineage without draining other work."""

    if not 1.0 <= timeout_seconds <= 3600.0:
        raise ValidationError("timeout_seconds must be between 1 and 3600")
    terminal_states = {"succeeded", "partial", "failed", "cancelled"}
    operator_states = {"paused", "pause_requested"}
    started_at = asyncio.get_running_loop().time()
    processed_items = 0
    scoped_job_ids = [job_id]
    await asyncio.to_thread(app.store.recover_expired_leases)

    stop_reason = "timeout"
    while True:
        discovery_index = 0
        while discovery_index < len(scoped_job_ids):
            current_id = scoped_job_ids[discovery_index]
            events = await asyncio.to_thread(
                app.store.list_events,
                current_id,
                limit=500,
            )
            for event in events:
                if event["event_type"] != "job.followup_created":
                    continue
                child_job_id = event["payload"].get("child_job_id")
                if (
                    isinstance(child_job_id, str)
                    and child_job_id not in scoped_job_ids
                ):
                    scoped_job_ids.append(child_job_id)
            discovery_index += 1

        jobs = [
            await asyncio.to_thread(app.store.get_job, scoped_id)
            for scoped_id in scoped_job_ids
        ]
        if all(job["status"] in terminal_states for job in jobs):
            stop_reason = "terminal"
            break
        if any(job["status"] in operator_states for job in jobs):
            stop_reason = "operator_controlled"
            break
        elapsed = asyncio.get_running_loop().time() - started_at
        if elapsed >= timeout_seconds:
            break
        processed_any = False
        for scoped_id, scoped_job in zip(scoped_job_ids, jobs, strict=True):
            if scoped_job["status"] in terminal_states | operator_states:
                continue
            processed = await app.worker.process_one(0, job_id=scoped_id)
            if processed:
                processed_items += 1
                processed_any = True
        if processed_any:
            continue
        await asyncio.sleep(min(1.0, max(0.1, app.settings.poll_seconds)))

    jobs = [
        await asyncio.to_thread(app.store.get_job, scoped_id)
        for scoped_id in scoped_job_ids
    ]
    root_job = jobs[0]
    deliveries_processed = 0
    if all(scoped_job["status"] in terminal_states for scoped_job in jobs):
        for scoped_id in scoped_job_ids:
            deliveries_processed += await app.delivery_worker.run_until_idle(
                job_id=scoped_id
            )
    results: list[dict[str, Any]] = []
    deliveries: list[dict[str, Any]] = []
    for scoped_id in scoped_job_ids:
        scoped_results = await asyncio.to_thread(
            app.store.list_results,
            scoped_id,
            limit=1000,
        )
        results.extend({"job_id": scoped_id, **result} for result in scoped_results)
        deliveries.extend(
            await asyncio.to_thread(
                app.store.list_deliveries,
                scoped_id,
                limit=1000,
            )
        )
    statuses = [scoped_job["status"] for scoped_job in jobs]
    if all(status == "succeeded" for status in statuses):
        lineage_status = "succeeded"
    elif results:
        lineage_status = "partial"
    elif statuses and all(status == "cancelled" for status in statuses):
        lineage_status = "cancelled"
    elif stop_reason == "terminal":
        lineage_status = "failed"
    else:
        lineage_status = "incomplete"
    return {
        "completed": stop_reason == "terminal",
        "lineage_status": lineage_status,
        "runner": {
            "mode": "job_scoped_in_process_worker",
            "started": True,
            "stopped_reason": stop_reason,
            "processed_items": processed_items,
            "deliveries_processed": deliveries_processed,
            "consumed_other_jobs": False,
            "root_job_id": job_id,
            "scoped_job_ids": scoped_job_ids,
        },
        "job": root_job,
        "jobs": jobs,
        "results": results,
        "deliveries": deliveries,
    }

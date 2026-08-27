---
name: operate-amazon-crawler
description: Create, inspect, pause, resume, cancel, and read resumable Amazon product, search, review, category, rank, and merchant crawl jobs through the project-local JSON CLI. Use when a user asks an AI agent to operate this crawler, collect one of its supported Amazon datasets, check task progress or evidence, or recover a paused crawl.
---

# Operate Amazon Crawler

Use `scripts/crawler_cli.py` for every operation. It fixes the project root, returns JSON, and blocks commands outside the safe operator surface.

## Workflow

1. Run `capabilities` before creating a job when the marketplace or input form is unclear.
2. Normalize the user's explicit scope into the selected task contract. Read [references/input-contracts.md](references/input-contracts.md) for non-product tasks.
3. Run `create`; preserve its returned `job.id`. Repeated identical requests are idempotent. Keep the default `sqlite` result sink unless the user requests another sink shown by `capabilities`.
4. Run `show` to report counts, item failures, checkpoint, and status. Use `events`, `results`, or `deliveries` only when evidence is requested.
5. Use `pause` for a safe stop. In-flight work may finish before status becomes `paused`.
6. Use `resume` only for `paused` or `pause_requested` jobs.
7. Use `cancel` only after the user explicitly confirms cancellation, and include `--confirm-cancel`.

## Commands

Run commands from the project root:

`B0XXXXXXXX` below is only an ASIN-shaped marker. Replace it with the authorized target product's real 10-character ASIN. Replace `JOB_ID` with the `job.id` returned by `create`.

```bash
python skills/operate-amazon-crawler/scripts/crawler_cli.py capabilities
python skills/operate-amazon-crawler/scripts/crawler_cli.py create B0XXXXXXXX --marketplace US --mode standard
python skills/operate-amazon-crawler/scripts/crawler_cli.py create --kind search --input-json '{"keyword":"wireless mouse","market_id":"US","post_code":"10001","turn_page":1,"frequent":0}'
python skills/operate-amazon-crawler/scripts/crawler_cli.py create B0XXXXXXXX --marketplace US --result-sink sqlite --result-sink jsonl
python skills/operate-amazon-crawler/scripts/crawler_cli.py show JOB_ID
python skills/operate-amazon-crawler/scripts/crawler_cli.py pause JOB_ID
python skills/operate-amazon-crawler/scripts/crawler_cli.py resume JOB_ID
python skills/operate-amazon-crawler/scripts/crawler_cli.py results JOB_ID
python skills/operate-amazon-crawler/scripts/crawler_cli.py deliveries JOB_ID
python skills/operate-amazon-crawler/scripts/crawler_cli.py cancel JOB_ID --confirm-cancel
```

Read [references/operations.md](references/operations.md) for states, outcome semantics, and reporting rules.

## Safety Rules

- Do not accept non-Amazon URLs, arbitrary request headers, cookies, proxies, credentials, or database paths from an Agent request.
- Accept only result sink names returned by `capabilities`; never accept a connection URL or filesystem path as a task option.
- Require explicit user confirmation before selecting any external MySQL or Redis result sink, and include `--confirm-external-result-write`. `sqlite` and `jsonl` do not need that flag.
- Do not run Cookie production, Cookie maintenance, task import/export, or external state-sync commands; those operations are outside this skill and require deployment authority.
- Do not start servers or workers through this skill.
- Do not claim that `pause` interrupts an in-flight HTTP request; it stops new claims and waits for current leases.
- Do not describe a crawl as failed merely because a secondary result delivery is retrying. Report crawl state and delivery state separately.
- Do not hide partial or failed items. Report error codes and attempts without exposing credentials or raw cookies.
- Do not change parser code, promote an evaluation candidate, or alter rate limits through this operator skill.
- Treat HTML evidence as potentially sensitive. Return hashes and bounded summaries unless the user explicitly requests the stored artifact.

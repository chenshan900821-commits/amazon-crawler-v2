# HTTP API v1

Base path: `/api/v1`

This P0 API is for local development. It must sit behind authentication, tenant authorization, quotas, and audit controls before any public deployment.

`B0XXXXXXXX` in the examples is a synthetic 10-character ASIN-shaped marker. Replace it with the target product's real ASIN before sending a request. Job IDs must come from the preceding create response; they are not caller-defined placeholders.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | readiness, redacted resource counts, and active HTTP/TLS transport |
| GET | `/capabilities` | plugins, marketplaces, modes and controls |
| GET | `/cookie-pools` | redacted Cookie acquisition feature and pool state |
| POST | `/cookie-pools/fill` | explicitly acquire validated cookies for one configured pool |
| POST | `/jobs` | create a job; stable idempotency applies except for un-timestamped `product_time` observations |
| GET | `/jobs` | list jobs; optional `status` and `limit` |
| GET | `/jobs/{id}` | job, checkpoint and item state |
| GET | `/jobs/{id}/results` | structured results and evidence metadata |
| GET | `/jobs/{id}/events` | bounded state event history |
| GET | `/jobs/{id}/deliveries` | secondary result delivery state and safe receipts |
| POST | `/jobs/{id}/pause` | safe pause request |
| POST | `/jobs/{id}/resume` | resume retained work |
| POST | `/jobs/{id}/cancel` | cancel remaining work |
| GET | `/metrics` | parser quality and failure distribution |

Product create example:

```json
{
  "kind": "product",
  "inputs": ["B0XXXXXXXX"],
  "marketplace_id": "US",
  "execution_mode": "standard",
  "postal_code": null,
  "priority": 0,
  "max_attempts": 5,
  "options": {
    "tags": ["validation"],
    "result_sinks": ["sqlite", "jsonl"]
  }
}
```

Cookie acquisition example:

```json
{
  "pool": "default",
  "marketplace_id": "US",
  "postal_code": "10001",
  "target_count": 10,
  "confirm_external_write": true
}
```

This operation is unavailable unless `CRAWLER_COOKIE_OPERATIONS_API_ENABLED=true` and the selected Redis Cookie pool is configured. It sends external Amazon requests and writes validated sessions to Redis, so the confirmation field is mandatory. One pool accepts only one operation at a time and `target_count` is limited to 1–50. The response contains counts and bounded failure codes only; it never returns Cookie values, proxy credentials, extraction URLs, or Redis connection details. These runtime secrets are not accepted as request fields either.

The Cookie endpoint is an operator-only control and is deliberately absent from the AI Agent Skill. Because this P0 server has no built-in login or tenant authorization, do not enable the endpoint on a publicly reachable service without an authenticated and audited control-plane boundary.

Structured search example:

```json
{
  "kind": "search",
  "inputs": [
    {
      "keyword": "wireless mouse",
      "market_id": "US",
      "post_code": "10001",
      "turn_page": 1,
      "frequent": 0
    }
  ],
  "execution_mode": "standard"
}
```

Supported canonical kinds are `product`, `product_hw`, `product_time`, `search`, `search_hour`, `reviews`, `category_asin_list`, `rank_list`, `merchant`, `merchant_home`, and `merchant_products`. Legacy JP aliases remain registered for migration.

Omitting `max_attempts` preserves the old retry budget: ordinary tasks receive 5 total attempts and `search_hour`/`search_hour_jp` receive 11. An explicit value from 1 to 20 overrides that default and participates in the idempotency identity.

For `product_time`, a request with no `add_date` means “observe now” and creates a new job on each submission. Supply `idempotency_key` when retrying the same observation request. Inputs that already contain `add_date` retain a stable observation identity.

`GET /health` reports `resources.transport.backend` and `resources.transport.tls_impersonation`. A clean production-equivalence environment must report `curl_cffi` and `true`; `httpx` means the process is running in the documented compatibility fallback and has not passed the TLS-fingerprint acceptance gate.

`GET /capabilities` reports configured result sink names. `sqlite` is always canonical; optional sinks are delivered from the transactional outbox after the crawl result commits. Callers may select only returned names through `options.result_sinks`; endpoints, database URLs, Redis keys, table names, credentials and output paths are rejected. Crawl status and delivery status are independent: a succeeded job can temporarily have `pending` or `dead_letter` secondary deliveries without losing its canonical result.

`merchant_home` creates its `merchant_products` page jobs atomically with the parent result. A `merchant_products` task carrying `source_task_id` creates its product-detail job atomically as well. Parent and child jobs remain independently visible and resumable; `job.followup_created` events provide the relationship.

The API never accepts Cookie values, proxies, request headers, Redis URLs, SQL, parser code, or arbitrary URLs. Product URLs, rank URLs, and optional merchant URLs are accepted only when HTTPS and on an allowlisted Amazon marketplace domain. Cookie and proxy health endpoints expose counts and state only, never credential values.

# Amazon Crawler MCP 内部观测与运行控制

这份手册描述 P0 单实例部署已经实施的可靠性控制、可观测输出和故障处理边界。所有阈值都是部署配置，不接受 MCP Tool 参数动态修改，避免 Agent 放宽保护。

## 控制矩阵

| 控制 | 实现位置 | 行为 |
| --- | --- | --- |
| 幂等 | Application Service + SQLite unique key | 调用方 Key 按租户命名；普通任务没有 Key 时按规范化输入生成稳定身份；并发重复只创建一个 Job。`product_time` 的立即观测需要调用方在重试时复用显式 Key |
| Worker 并发 | Crawl Worker / Delivery Worker | 固定 Slot 数、租约、心跳和 lease token；过期租约可恢复，旧 Worker 的迟到结果被拒绝 |
| MCP 同步并发 | MCP Execution Policy | `crawler_run_job` 使用 Semaphore；短暂排队后快速拒绝，建议改为异步 Job |
| 上游超时 | HTTP Fetcher | 每个请求使用 `CRAWLER_REQUEST_TIMEOUT_SECONDS`，超时分类为可重试网络故障 |
| 有限重试 | Job Item / Result Outbox | 普通抓取默认最多 5 次，小时任务 11 次，显式上限 20；结果投递使用独立最大次数 |
| 指数退避 | SQLite 状态机 | 抓取按 `min(300, 2^(attempt-1))` 秒；结果投递按 `min(900, 2^(attempt-1))` 秒 |
| 熔断 | Host Circuit Breaker | 网络错误、403/429、可重试 5xx 累积；开路期间拒绝，恢复窗口后只允许一个探针 |
| 分页 | MCP list/get Tool | 使用不透明 `next_cursor`，服务端强制最大页大小 |
| 输出限制 | MCP Response Bounder | 限制序列化字节；超大单条改为哈希、大小和截断说明 |

## 观测入口

### 内部 MCP 控制面

MCP 指标、告警和审计不注册为 Agent Tool。默认情况下
`/internal/mcp/*` 返回 404；开启内部观测必须同时配置：

```dotenv
CRAWLER_MCP_INTERNAL_OBSERVABILITY_ENABLED=true
CRAWLER_MCP_INTERNAL_OBSERVABILITY_TOKEN_SHA256=运维Token的小写SHA256摘要
```

内部监控携带原始运维 Token 读取：

- `GET /internal/mcp/observability`：MCP 调用量、成功/失败、进行中、限流、并发拒绝、p50/p95/最大延迟、Tool/错误分布、内部告警和实际保护上限；
- `GET /internal/mcp/audit`：调用者、租户、Tool、参数摘要、结果、错误和耗时；支持 `tenant_id`、`limit`、`cursor`。

运维 Token 与业务 OAuth Token 相互独立。生产网关必须额外限制
`/internal/mcp/*` 的来源网络，响应禁止缓存。运行指标是单进程有界窗口，受
`CRAWLER_MCP_METRICS_MAX_SAMPLES` 限制并在重启后归零；审计事实保存在 SQLite。

### HTTP 探针

- `GET /health/live`：进程存活，200。
- `GET /health/ready`：数据库正常时 200、不可用时 503；只返回最小 `status`。

对外 `crawler_health` 只返回 `ok`、`status`、`accepting_jobs`，不返回 MCP 指标、告警、部署限制或审计事件。

## 结构化日志

stderr 每行一个 JSON 对象，常见事件：

- `mcp_request_completed`
- `crawl_worker_started` / `crawl_worker_stopped`
- `crawl_item_claimed` / `crawl_item_completed` / `crawl_item_failed`
- `result_delivery_claimed` / `result_delivery_completed` / `result_delivery_failed`
- `crawler_alert_state_changed`
- `crawler_alert_delivered` / `crawler_alert_delivery_failed`

日志不记录调用参数；持久化 MCP 审计表只保存参数 SHA-256。日志和审计都不记录 Cookie、Token、Authorization、代理 URL、签名密钥或 Webhook URL。`CRAWLER_LOG_LEVEL` 默认 `INFO`，生产不建议降低到 `DEBUG` 后采集第三方库的原始 HTTP 日志。

## 默认告警

| Code | 默认条件 | 建议处理 |
| --- | --- | --- |
| `database_unavailable` | MCP 状态/审计 SQLite healthcheck 失败 | 停止创建任务，检查磁盘、权限、锁和数据库完整性 |
| `mcp_error_rate_high` | 窗口至少 20 次且失败率 ≥ 25% | 按错误代码区分权限、参数、上游或服务异常 |
| `mcp_latency_high` | 窗口至少 20 次且 p95 ≥ 5000ms | 检查同步长任务、数据库、Worker 和上游延迟 |
| `mcp_rate_limit_high` | 窗口限流拒绝 ≥ 10 | 检查 Client 重试循环，使用 `retry_after_seconds` |
| `mcp_concurrency_saturated` | 同步并发拒绝 ≥ 3 | 改用 `crawler_create_job` + Worker，不要简单增大进程内上限 |

阈值对应 `.env.example` 中的 `CRAWLER_MCP_ALERT_*`。状态只在 active/resolved 变化时通知，不会在每次探针轮询时重复发送。

## Webhook

配置 `CRAWLER_MCP_ALERT_WEBHOOK_URL` 后，服务发送：

```json
{
  "schema": "amazon_crawler_alert_v1",
  "occurred_at": "2026-08-29T00:00:00+00:00",
  "tenant_hash": "16位不可逆摘要",
  "alert": {
    "state": "active",
    "code": "mcp_error_rate_high",
    "severity": "warning",
    "message": "MCP call error rate exceeds the configured threshold",
    "observed": 0.31,
    "threshold": 0.25
  }
}
```

生产 Webhook 必须使用 HTTPS。完整 URL按 Secret 管理；服务不记录它。发送在后台执行，不阻塞 MCP 调用或健康探针，使用短超时、最多 1–5 次（默认 3 次）和 `0.25/0.5/1/2` 秒指数退避，不跟随重定向。结构化 active/resolved 日志始终先写出；进程在后台发送完成前被强制终止时，Webhook 可能丢失，因此平台日志告警仍应保留。

## 告警后的原则

1. `created=true` 只证明 Job 已持久化，不是抓取成功。
2. 运维人员先查询内部 MCP 观测、审计和具体 Job 事件，再决定重试。
3. 重试同一业务请求必须复用原 `idempotency_key`。
4. 限流或并发饱和期间不要循环调用同步 Tool；遵守 `retry_after_seconds`。
5. dead-letter 必须先确认目标系统是否已经写入，再决定重放。
6. SQLite P0 是单实例；扩容前必须把数据库、限流、并发额度和告警去重迁移到共享控制面。
7. P0 告警去重状态保存在进程内；服务重启后，仍然成立的告警会重新发送一次。

# 真实运行证据与复现说明

本页回答三个问题：执行的是什么、实际抓到了什么、别人怎样按同样步骤复现。2026-08-30 的四次验证使用四个独立 Job，请求 Amazon US 的公开商品 `B07FZ8S74R`，配送邮编 `10001`。Cookie、代理、Redis URL 和 Token 只存在于本机 `.env`，没有出现在命令、截图、日志或仓库中。

## 先说明证据类型

- `live/*.typescript` 是终端程序当次 stdout/stderr 的原始记录，没有重新编写成功输出；其中 Agent 记录包含 Codex 的 Skill 读取、MCP Tool 调用参数、Tool 返回值和最终回答。
- `live/web-*.png` 是真实页面按顺序操作时保存的浏览器截图，最终截图直接显示商品字段、HTTP 证据和完整结构化 JSON。
- README 中原有 GIF/MP4 是依据 2026-08-29 真实运行结果制作的压缩时间证据回放，不是未经剪辑的连续屏幕录像。它们用于快速预览；要审计真实性，应以本页的 2026-08-30 原始记录、JSON 收据和独立 Job 为准。
- 本次新增 Agent、MCP、Web 和 CLI 四条回放：Agent/MCP/CLI 由各自本次原始事件流或终端记录生成，Web 由本次浏览器逐步截图生成；四条都在画面和文档中标明“回放”。
- 视频 SHA-256：Agent `6980d86c8446ca86493b7e89f1afdbe13276d5c37475ce9aa01139690040d8a3`；MCP `dce56c0b262eba604b27d55e042926328021e4cec20d023e9f9b19a55856298b`；Web `a02dcd13b474c7e2a3cbf88c283e428388b3fefcdf1c2e8aded337d082ac7787`；CLI `afe6ed69d04090735b6869103d83e130537dcc9bf9ecd9f46b87662fa765d91c`。Agent 原始事件流 SHA-256 为 `fea2d205bdcfb65d01b4fd22681b1eb80cea2130e3b0e01b7078bcb1223a9de3`。
- Amazon 页面会变化，评论数、价格、响应字节数和 SHA-256 不应写成固定测试断言。

## 四条独立任务

| 入口 | Job ID | 终态 / 尝试 | 可见结果 | HTTP 证据 |
|---|---|---|---|---|
| Codex Agent + Skill + MCP | `job_b32ad252512649999fed195f8b93d3eb` | `succeeded` / 1 | 标题、品牌、评分、评论数、schema | `200` / `1,575,322` bytes / `126577…3710` |
| MCP STDIO Client | `job_370230bb081c45ec9602bda4bee01f64` | `succeeded` / 1 | 同上 | `200` / `1,571,807` bytes / `0ee1ec…5191` |
| Web 管理页 | `job_a0874e10fdf545b4b3934d2a8a613775` | `succeeded` / 1 | 同上，并可展开完整 JSON | `200` / `1,566,797` bytes / `cf712e…0fd4` |
| CLI | `job_d8e5065844da48c9bc64a06fda0ca387` | `succeeded` / 1 | 同上 | `200` / `1,563,014` bytes / `67c2a3…5099` |

四次都解析出：

```json
{
  "asin": "B07FZ8S74R",
  "title": "Echo Dot (3rd Gen, 2018 release) - Smart speaker with Alexa - Charcoal",
  "brand": "Amazon",
  "rating": "4.7 out of 5 stars",
  "schema_version": "amazon.product.v2"
}
```

完整、不省略的哈希和当次评论数见各入口的 JSON 收据：

- [`agent-mcp-receipt.json`](assets/demos/live/agent-mcp-receipt.json)
- [`mcp-client-receipt.json`](assets/demos/live/mcp-client-receipt.json)
- [`web-receipt.json`](assets/demos/live/web-receipt.json)
- [`cli-receipt.json`](assets/demos/live/cli-receipt.json)

## README 最终复验

四条演示完成后，又严格按 README 的 CLI 主流程创建了一条全新任务，而不是读取上面的已有 Job：

| Job ID | 终态 / 尝试 | 结果 | HTTP 证据 | Runner 边界 |
|---|---|---|---|---|
| `job_199e5bd78655453caf5d779d21831349` | `succeeded` / 1 | 1 条，标题/品牌/评分均非空 | `200` / `1,567,022` bytes / `0f3b2e…170` | `terminal`，未消费其他 Job |

完整收据：[`readme-final-verification-receipt.json`](assets/demos/live/readme-final-verification-receipt.json)。这说明仓库当前 README 的自包含命令确实能启动任务范围内的 Worker、完成实时请求并返回可审计结果；仍然只代表本次受控本机样本，不代表公网生产 SLA。

## 从零复现

先完成 README 的“5 分钟拿到第一条结果”，确保：

```bash
amazon-crawler doctor
```

返回 `configuration_ready=true`。这只证明配置结构完整，真实可用性仍要由下面任务的终态、非空结果和 HTTP 证据证明。

### 1. Codex Agent + Agent Skill + MCP

仓库已提供项目级 [`.codex/config.toml`](../.codex/config.toml)。用户信任仓库后，Codex 会按配置启动 `scripts/start_mcp_stdio.py`；启动器只安全读取 `.env` 中的 `CRAWLER_*`，不会执行 `.env`，也不需要把秘密粘贴到对话中。打开本仓库的 Codex 任务并输入：

```text
$operate-amazon-crawler 使用已经连接的 amazon-crawler MCP 工具真实抓取 Amazon US 商品 B07FZ8S74R，邮编 10001，kind=product_time，max_attempts=3。必须先调用 crawler_doctor；配置可用后调用 crawler_run_job。最后报告 job id、终态、结果数、商品标题、品牌、评分、评论数、HTTP 状态、响应字节数、SHA-256、采集时间和 schema。
```

当次真实 Agent 原始事件流：[`agent-codex-mcp-success-20260830.typescript`](assets/demos/live/agent-codex-mcp-success-20260830.typescript)。可以在其中搜索以下事件，逐步核对：

```text
"tool":"crawler_doctor"
"tool":"crawler_run_job"
"tool":"crawler_get_job"
"tool":"crawler_get_results"
"lineage_status":"succeeded"
job_b32ad252512649999fed195f8b93d3eb
```

这份记录证明 Agent 确实执行了 Skill 编排，并通过 MCP Tool 抓取和二次读取持久化结果；不是只运行了 Skill 的后备脚本。

### 2. MCP Server + Client

本机 STDIO Client 会自动启动 Server，不需要先运行网页或 Worker：

```bash
amazon-crawler-mcp-client smoke
amazon-crawler-mcp-client call crawler_run_job \
  --arguments '{"inputs":["B07FZ8S74R"],"kind":"product","marketplace_id":"US","postal_code":"10001","max_attempts":3,"idempotency_key":"换成本次请求的唯一编号","timeout_seconds":240}'
```

检查 `result.is_error=false`、`structured_content.lineage_status=succeeded`、`results` 非空和 `evidence.http_status=200`。当次原始记录：[`mcp-20260830.typescript`](assets/demos/live/mcp-20260830.typescript)。

### 3. Web 页面

```bash
amazon-crawler serve --host 127.0.0.1 --port 3000
```

打开 <http://127.0.0.1:3000>，按下面顺序操作：

1. 类型选择“商品实时观测”，模式选择 `Realtime`。
2. 输入 `B07FZ8S74R`、站点 `US`、邮编 `10001`、最大尝试次数 `3`。
3. 点击“创建任务”，打开任务详情；详情会从“待执行/执行中”自动刷新到“已完成”。
4. 在结果卡直接查看标题、品牌、评分、评论数、HTTP 状态、字节数和完整 SHA-256；点击“查看本条完整结构化 JSON”可核对全部字段。

实际操作截图按顺序保存：[`填写完成`](assets/demos/live/web-02-filled.png) → [`任务执行中`](assets/demos/live/web-04-running.png) → [`自动刷新后的结果`](assets/demos/live/web-09-auto-refreshed-result.png) → [`HTTP 与 SHA-256`](assets/demos/live/web-07-http-evidence.png) → [`完整 JSON`](assets/demos/live/web-08-json-expanded.png)。

### 4. CLI

CLI 的 `run` 自己启动任务范围内的临时 Worker，不需要网页或常驻 Worker：

```bash
amazon-crawler run B07FZ8S74R \
  --kind product_time \
  --marketplace US \
  --postal-code 10001 \
  --max-attempts 3 \
  --idempotency-key "换成本次请求的唯一编号" \
  --timeout-seconds 240
```

当次原始记录：[`cli-20260830.typescript`](assets/demos/live/cli-20260830.typescript)。

## 怎样判断不是“假成功”

必须同时满足：

1. `completed=true` 且 `lineage_status=succeeded`；
2. Job 为 `succeeded`，成功数大于 0、失败数为 0；
3. `results` 非空，并有实际商品标题等业务字段；
4. `evidence.http_status=200`，同时记录响应字节数与 SHA-256；
5. 自包含运行时 `runner.started=true`、`runner.stopped_reason=terminal`、`runner.consumed_other_jobs=false`。

`created=true` 只表示任务已持久化，MCP 握手成功只表示协议链路可用，二者都不能单独证明抓取成功。

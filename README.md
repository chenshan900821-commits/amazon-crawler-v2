# Amazon Crawler V2

面向 Amazon 数据采集场景的可恢复、可观测爬虫服务。项目提供管理界面、HTTP API、命令行和 Agent Skill，统一承载任务创建、断点续爬、暂停/恢复、结果查询、多存储投递以及 Cookie/代理资源管理。

> 当前阶段：P0 核心能力和离线测试已完成，可以用于本地开发与受控联调。在补齐认证、租户隔离、限流、配额和真实站点验收前，不应直接作为公网生产服务。

## 先看这里：从安装到拿到结果

这一节是项目的唯一主运行入口。第一次使用时按顺序执行即可；后面的章节用于解释配置、任务类型和部署方式。

### 1. 安装

环境要求：Python 3.11+，推荐 macOS、Linux 或 WSL2。

```bash
git clone https://github.com/chenshan900821-commits/amazon-crawler-v2.git
cd amazon-crawler-v2
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

### 2. 准备并加载配置

```bash
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，至少配置一种已获授权的 Amazon Cookie 来源：

- Redis Cookie 池：`CRAWLER_COOKIE_REDIS_URL`，推荐用于持续运行。
- 静态 Cookie：`CRAWLER_AMAZON_COOKIE`，适合单会话联调。

代理不是按“出口 IP 会不会变化”来选，而是按代理服务商交给你的连接方式来选：

| 服务商交给你的内容 | 配置哪个变量 | 程序如何使用 |
|---|---|---|
| 一个“提取代理”的 API 链接；访问链接后返回 `host:port` | `CRAWLER_PROXY_EXTRACT_URL` | 代理池为空时调用该链接，取得新的代理地址 |
| 一个可以直接连接的完整代理/隧道 URL（由协议、隧道账号、密码、网关主机和端口组成） | `CRAWLER_HTTP_PROXY` | 每次请求都连接这个地址 |

例如，青果给你的是“提取链接”，就配置 `CRAWLER_PROXY_EXTRACT_URL`；给你的是隧道网关、主机和端口，就拼成完整 URL 配置 `CRAWLER_HTTP_PROXY`。即使隧道背后的出口 IP 自动变化，只要程序始终连接同一个网关，它仍属于 `CRAWLER_HTTP_PROXY`。通常只配置一种；两种同时配置时，程序只使用动态提取接口，不会在提取失败后回退到固定代理。

每个占位符具体代表什么，见[配置真实抓取资源](#配置真实抓取资源)。真实 Cookie、代理凭证和数据库连接只放在本机 `.env` 或部署平台 Secret 中。

项目不会自动读取 `.env`。每次打开新终端，都先执行：

```bash
source .venv/bin/activate
set -a
source .env
set +a
```

先运行脱敏配置检查。它只检查是否缺项和格式是否合理，不连接 Amazon、Redis 或代理，也不会显示任何秘密值：

```bash
amazon-crawler doctor
```

只有 `configuration_ready=true` 才表示必填配置已经补齐；它不等于真实网络已经连通。若为 `false`，按 `blocking_issues` 列出的环境变量名修改 `.env`，重新加载后再检查。

然后初始化任务数据库：

```bash
amazon-crawler init-db
```

默认数据库是 `.data/crawler.db`。同一套 API、CLI 和 Worker 必须使用同一个 `CRAWLER_DB_PATH`。

### 3A. 使用页面运行

```bash
amazon-crawler serve --host 127.0.0.1 --port 3000
```

打开 <http://127.0.0.1:3000>。`serve` 默认同时启动页面、API、抓取 Worker 和结果投递 Worker。

在页面点击“创建任务”后，任务会先持久化为 `pending`，随后由 Worker 自动领取并执行。页面关闭不会中断任务，但上面的服务进程必须保持运行。如果已有任务正在执行，新任务会在队列中等待。

健康检查：

```bash
curl http://127.0.0.1:3000/api/v1/health
curl http://127.0.0.1:3000/api/v1/capabilities
```

### 3B. 完全不使用页面

创建一个商品任务。示例 ASIN 必须替换成目标商品 `/dp/` 后真实的 10 位 ASIN：

```bash
amazon-crawler create B07FZ8S74R \
  --kind product \
  --marketplace US \
  --postal-code 10001
```

如果没有运行 `serve` 或常驻 Worker，再执行一轮队列：

```bash
amazon-crawler worker --once
```

`worker --once` 会处理当前可领取的抓取任务和结果投递，然后退出。长期无人值守运行使用：

```bash
amazon-crawler worker
```

常驻 Worker 只负责消费队列；定时创建新任务可以由 Cron、systemd timer 或其他调度平台调用 `amazon-crawler create`。`product_time` 适合周期性实时观测；普通 `product` 的相同请求默认幂等，不会因重复提交而反复采集。

### 4. 查看任务和结果

`create` 命令返回的任务编号位于 `job.id`。把真实编号代入：

```bash
JOB_ID='JOB_ID_FROM_CREATE_RESPONSE'
amazon-crawler show "$JOB_ID"
amazon-crawler events "$JOB_ID"
amazon-crawler results "$JOB_ID"
amazon-crawler deliveries "$JOB_ID"
```

页面运行时，也可以直接在“任务编队”中打开任务详情。

| 状态 | 含义 |
|---|---|
| `pending` | 已保存，等待 Worker 领取 |
| `running` | Worker 正在抓取 |
| `succeeded` | 全部输入已结束；仍应确认结果数量大于 0 |
| `partial` | 已有可用结果，但存在失败输入 |
| `failed` | 没有形成可用的完整结果，应查看 `events` 中的失败码 |

SQLite 标准结果保存在 `.data/crawler.db`；选择 `jsonl` 结果去向后，增量文件保存在 `.data/result-sinks/jsonl`。

### 5. 停止与再次启动

在前台进程中按 `Ctrl+C` 安全停止。再次打开终端后，重新加载 `.env`，然后运行 `amazon-crawler serve` 或 `amazon-crawler worker`。任务状态和断点保存在 SQLite 中；Worker 会恢复可继续处理的任务，不要求浏览器保持打开。

## 三种运行方式

| 方式 | 启动命令 | 谁创建任务 | 适用场景 |
|---|---|---|---|
| 页面与 Worker 一体 | `amazon-crawler serve` | 页面、CLI 或 API | 本地使用、单机受控部署 |
| 纯命令行单次运行 | `amazon-crawler create ...` 后执行 `amazon-crawler worker --once` | CLI | 脚本、批处理、调试 |
| 常驻 Worker | `amazon-crawler worker` | CLI、API 或外部调度器 | 无页面、定时或持续运行 |

## 使用 Agent Skill

仓库级 Skill 位于 `.agents/skills/operate-amazon-crawler`，其内容指向 `skills/operate-amazon-crawler` 中的唯一实现。Codex 从仓库根目录或子目录启动时，可以按官方约定发现它；如果刚克隆或更新后没有出现，重启 Codex。目录规则见 [OpenAI Skills 文档](https://developers.openai.com/codex/skills)。

在 Codex 中可以显式调用：

```text
$operate-amazon-crawler 为 US 站点创建一个已获授权的商品采集任务，并报告任务状态和结果数量。
```

Agent Skill 只允许创建、查看、暂停、恢复、取消以及读取任务证据，不允许启动 Worker、生产 Cookie、修改代理或接收数据库路径。调用 Skill 之前，运维人员必须已经运行以下任一种执行进程：

```bash
amazon-crawler serve
# 或者
amazon-crawler worker
```

两者必须与 Skill 使用相同的 `CRAWLER_DB_PATH`。`create` 返回 `created: true` 只表示任务已经持久化；继续用 `show` 确认它从 `pending` 进入 `running` 或终态。如果一直是 `pending`，先检查 Worker，不要反复创建相同任务。

Skill 每次创建任务前都会先做与 `amazon-crawler doctor` 相同的脱敏检查。缺少 Cookie 来源、代理字段只填一半或 URL 格式明显错误时，它不会创建任务，而会返回 `blocking_issues`，其中只包含需要配置的环境变量名和操作说明。用户应在项目根目录 `.env` 或部署平台 Secret 中填写真实值，再加载配置；不要把 Cookie、提取链接、账号密码或 Redis URL 发给 Agent。

不依赖 Agent 界面时，可以直接验证 Skill 的确定性包装脚本：

```bash
python skills/operate-amazon-crawler/scripts/crawler_cli.py doctor
python skills/operate-amazon-crawler/scripts/crawler_cli.py capabilities
python skills/operate-amazon-crawler/scripts/crawler_cli.py create B0XXXXXXXX --marketplace US
python skills/operate-amazon-crawler/scripts/crawler_cli.py show JOB_ID
python skills/operate-amazon-crawler/scripts/crawler_cli.py results JOB_ID
python skills/operate-amazon-crawler/scripts/crawler_cli.py events JOB_ID
```

搜索任务的 `results` 外层是一条“页面结果信封”，真实搜索行数在 `results[0].data.row_count`，商品列表在 `results[0].data.items`；不能把外层数组长度误当成搜索商品数量。完整安全边界和输入契约见 [`skills/operate-amazon-crawler/SKILL.md`](skills/operate-amazon-crawler/SKILL.md)。

## 需要新 Cookie 时

如果 Redis 池已经满足容量，不需要先生产新 Cookie，可以直接创建抓取任务。确实需要补池时，可以使用页面中的“Amazon Cookie 资源”，也可以调用同一生产内核：

```bash
amazon-crawler cookie-fill \
  --pool default \
  --marketplace US \
  --target 1 \
  --confirm-external-write
```

站点的配送区域由配置自动选择。只有 `satisfied=true` 且 `report.available_after >= report.requested` 才表示目标容量已满足；`created=0` 也可能只是池内原有容量已经足够。Cookie 生产会访问 Amazon 并写入 Redis，必须先确认相关访问与写入已获授权。

## 项目能力

Amazon 数据采集涉及任务调度、资源管理、页面解析、失败恢复和结果投递。本项目将它们收敛为一个可恢复的服务：

| 能力 | 说明 |
|---|---|
| 断点续爬 | Job、输入项、连续断点、租约、尝试次数和事件保存在 SQLite WAL 中 |
| 任务控制 | 支持创建、查看、暂停、恢复、取消和幂等提交 |
| Worker | 通过租约、心跳和 lease token 协调并发，过期任务可重新领取 |
| 多结果存储 | SQLite 为标准结果原本，可通过事务 outbox 投递到 JSONL、Redis 或 MySQL |
| Cookie 与代理 | 支持静态 Cookie、Redis Cookie 池、配送区域、代理提取、隔离和刷新 |
| 可观测性 | 提供任务事件、结果、投递状态、指标、响应哈希和可选脱敏证据 |
| 多种入口 | 提供 Web 管理界面、REST API、CLI 和 Agent Skill |

## 支持的任务

| Kind | 用途 |
|---|---|
| `product` | 标准商品详情 |
| `product_hw` | 使用 overseas 资源池的商品详情 |
| `product_time` | 带独立观测时间的实时商品数据 |
| `search` | 关键词搜索结果 |
| `search_hour` | 带小时批次身份的高频搜索结果 |
| `reviews` | 商品评论 |
| `category_asin_list` | 类目 ASIN 列表 |
| `rank_list` | Amazon 榜单数据 |
| `merchant` | 商家信息 |
| `merchant_home` | 商家首页和派生分页任务 |
| `merchant_products` | 商家商品页和派生商品详情任务 |

## 系统结构

```mermaid
flowchart LR
    Caller[Web UI / CLI / API / Agent Skill] --> Service[Application Service]
    Service --> Store[(SQLite WAL<br/>Job / Checkpoint / Event / Result)]
    Worker[Crawl Worker] --> Store
    Worker --> Resources[Cookie / Proxy / Rate Limit]
    Resources --> Amazon[Amazon]
    Amazon --> Parser[Amazon Plugins / Parsers]
    Parser --> Worker
    Store --> Outbox[Transactional Outbox]
    Outbox --> Delivery[Delivery Worker]
    Delivery --> JSONL[JSONL]
    Delivery --> Redis[Redis]
    Delivery --> MySQL[MySQL]
```

抓取成功与 outbox 在同一个 SQLite 事务中提交。二级存储暂时不可用时，SQLite 中的标准结果不会丢失，投递 Worker 会单独重试。

## 配置真实抓取资源

运行时只读取进程环境变量，不会自动加载 `.env`。复制、加载和启动命令统一见[从安装到拿到结果](#先看这里从安装到拿到结果)，这里仅解释各配置项的真实含义。

仓库中的 [`.env.example`](.env.example) 只定义字段、格式和安全默认值，必须保持为占位符或空值。本机 `.env` 已被 Git 忽略；正式部署应使用部署平台的 Secret 管理功能，不要把 Cookie、代理账号、提取链接或 Redis URL 写进镜像、启动脚本、Job API 或 Agent 参数。

### Amazon Cookie 填什么

`CRAWLER_AMAZON_COOKIE` 需要的是目标 Amazon 站点一次有效请求中的完整 `Cookie` 请求头，不是 `Set-Cookie` 响应头，也不是 Amazon 账号密码。

取得方式：在已获授权的浏览器会话中打开对应 Amazon 站点，使用开发者工具的 Network 面板选择一个正常返回的文档请求，在 Request Headers 中复制完整 `Cookie` 值。Cookie 必须与目标站点、配送邮编和会话区域匹配。

支持两种格式：

```bash
# 推荐：完整 Cookie 请求头。放进 .env 时使用单引号包住整段内容。
CRAWLER_AMAZON_COOKIE='session-id=SESSION_ID_VALUE; ubid-main=UBID_VALUE; lc-main=en_US'

# 也支持 JSON 对象。
CRAWLER_AMAZON_COOKIE='{"session-id":"SESSION_ID_VALUE","ubid-main":"UBID_VALUE","lc-main":"en_US"}'
```

上面的 `SESSION_ID_VALUE` 和 `UBID_VALUE` 是格式标记，必须替换为本人已授权会话中的实际值，不能原样使用。不同站点的 Cookie 名可能不同，因此不要只复制示例中的三个字段；优先复制完整请求头。

`CRAWLER_MERCHANT_COOKIE` 使用相同格式，仅在商家和榜单任务需要独立会话时配置。未配置 Redis Cookie 池时，普通任务使用 `CRAWLER_AMAZON_COOKIE`。

### 代理先判断：提取 API 还是直接连接地址

判断方法只看你从服务商控制台拿到什么：

- 点击或请求某个链接后，服务商才返回一批 `IP:端口`：这是“提取 API”，配置 `CRAWLER_PROXY_EXTRACT_URL`。
- 服务商直接给出“网关主机 + 端口”，程序可以把它当代理连接：这是“直接连接地址”，配置 `CRAWLER_HTTP_PROXY`。
- “动态 IP 隧道”通常仍有固定网关。虽然出口 IP 变化，但程序连接的网关没变，所以配置 `CRAWLER_HTTP_PROXY`，不要配置提取 URL。

两种方式通常二选一。若同时配置，`CRAWLER_PROXY_EXTRACT_URL` 优先，`CRAWLER_HTTP_PROXY` 不作为备用线路。

### 直接连接的代理或隧道填什么

拿到一个可直接连接的代理 IP 或隧道网关时，配置完整代理 URL：

```bash
# 无认证代理
CRAWLER_HTTP_PROXY='http://PROXY_HOST:PROXY_PORT'
```

带认证的固定代理使用标准 URL userinfo 结构，将下面三段按顺序拼接，并把完整结果作为 `CRAWLER_HTTP_PROXY` 的值：

1. scheme：`http://`
2. 认证部分：`PROXY_USER:PROXY_PASSWORD@`
3. 地址部分：`PROXY_HOST:PROXY_PORT`

- `PROXY_HOST`：代理服务商分配的 IP 或域名。
- `PROXY_PORT`：代理端口，例如隧道代理端口。
- `PROXY_USER` / `PROXY_PASSWORD`：代理隧道认证账号和密码，不是服务商网站的登录账号。
- 用户名或密码包含 `@`、`:`、`/` 等特殊字符时，需要先进行 URL percent-encoding。

推荐使用 `http://` 或 `https://` 代理 URL。不要把代理地址直接写入代码。

### 提取型动态代理填什么（包括青果提取 API）

使用青果的提取型代理时，配置以下三个通用变量，不需要修改 Python 代码：

```bash
CRAWLER_PROXY_EXTRACT_URL='PASTE_FULL_QINGGUO_EXTRACTION_API_URL_HERE'
CRAWLER_PROXY_USERNAME='QINGGUO_TUNNEL_USERNAME'
CRAWLER_PROXY_PASSWORD='QINGGUO_TUNNEL_PASSWORD'
```

- `CRAWLER_PROXY_EXTRACT_URL`：从青果控制台生成的完整代理提取链接。链接中的 token、签名或白名单参数都属于秘密。
- `CRAWLER_PROXY_USERNAME`：青果代理隧道认证用户名。
- `CRAWLER_PROXY_PASSWORD`：青果代理隧道认证密码。
- 提取接口必须以纯文本返回代理，每行一个 `host:port`、`http://host:port` 或 `https://host:port`；当前不接受 HTML 或 JSON 响应。

这里的用户名和密码用于认证“提取出来的代理地址”。如果青果给你的是一个可直接连接的隧道网关，而不是返回 `host:port` 的提取 API，就不要使用这三个变量；应将隧道账号、密码、网关和端口拼成完整 URL，写入 `CRAWLER_HTTP_PROXY`。

如果青果采用 IP 白名单而不要求隧道账号，用户名和密码保持为空。配置 `CRAWLER_PROXY_EXTRACT_URL` 后，动态代理池优先于 `CRAWLER_HTTP_PROXY`；提取失败时系统会进入冷却并报告资源错误，不会偷偷改成直连。

当前一个服务进程只能使用一套动态代理提取链接和一组隧道凭证，并由所有任务共享。如果不同站点或任务必须使用不同青果线路，当前版本还需要增加按站点/任务类型路由的 Proxy Provider；不能仅启动多个 Worker 就假定任务会进入正确线路。

### Redis Cookie 池填什么

需要按站点和邮编管理多个 Cookie 时，配置完整 Redis 连接 URL：

```bash
CRAWLER_COOKIE_REDIS_URL='redis://REDIS_HOST:REDIS_PORT/REDIS_DB'
CRAWLER_COOKIE_REDIS_OVERSEAS_URL='redis://REDIS_HOST:REDIS_PORT/OVERSEAS_DB'
```

Redis 需要密码时，将下面三段按顺序拼接，并把完整结果写入对应变量：

1. scheme：`redis://`
2. 认证部分：`:REDIS_PASSWORD@`
3. 地址部分：`REDIS_HOST:REDIS_PORT/REDIS_DB`

- `CRAWLER_COOKIE_REDIS_URL`：default Cookie 池。
- `CRAWLER_COOKIE_REDIS_OVERSEAS_URL`：`product_hw` 以及 JP 资源路由使用的 overseas Cookie 池。
- `REDIS_HOST` / `REDIS_PORT`：Redis 服务的域名或 IP 与端口。
- `REDIS_PASSWORD`：Redis 连接密码；无密码部署时删除 URL 中的 `:REDIS_PASSWORD@`。
- `REDIS_DB` / `OVERSEAS_DB`：Redis database 编号，例如 `0`、`4`，不是数据库名称。
- Redis key 格式为 `cookie:{marketplace}:{postal_code}:{id}`。
- Redis value 可以是 Cookie JSON 对象，也可以是完整 Cookie 请求头字符串。
- 使用 TLS Redis 时将 scheme 改为 `rediss://`。

Redis 密码包含特殊字符时同样需要 percent-encoding。只要配置了 default Redis Cookie 池，它就优先于 `CRAWLER_AMAZON_COOKIE`。

### 其余运行参数

| 环境变量 | 默认值 | 什么时候修改 |
|---|---:|---|
| `CRAWLER_DB_PATH` | `.data/crawler.db` | 修改 SQLite 持久化位置时 |
| `CRAWLER_EVIDENCE_DIR` | `.data/evidence` | 修改脱敏证据目录时 |
| `CRAWLER_RESULT_JSONL_DIR` | `.data/result-sinks/jsonl` | 修改 JSONL 结果目录时 |
| `CRAWLER_CAPTURE_EVIDENCE` | `false` | 需要保存解析失败证据时开启 |
| `CRAWLER_WORKER_ENABLED` | `true` | API 与 Worker 分离部署时设为 `false` |
| `CRAWLER_WORKER_CONCURRENCY` | `2` | 根据代理容量和限流策略调整抓取并发 |
| `CRAWLER_DELIVERY_WORKER_ENABLED` | `true` | 独立运行结果投递 Worker 时设为 `false` |
| `CRAWLER_REQUEST_TIMEOUT_SECONDS` | `25` | 网络延迟较高时适当增加 |
| `CRAWLER_MIN_HOST_INTERVAL_SECONDS` | `1.5` | 控制同一站点的最小请求间隔 |
| `CRAWLER_HTTP_TRANSPORT` | `auto` | 正常保持 `auto`；`httpx` 仅用于诊断 |
| `CRAWLER_USER_AGENT` | Chrome 131 UA | 一般不修改；必须与 TLS/browser 模拟配置保持一致 |
| `CRAWLER_REQUIRE_COOKIE` | `true` | 正常抓取保持 `true`；无 Cookie 模式仅限受控诊断 |
| `CRAWLER_COOKIE_OPERATIONS_API_ENABLED` | `false` | 只在管理服务已限制为授权运维人员访问时开启页面/API 获取功能 |
| `CRAWLER_PROXY_QUARANTINE_SECONDS` | `300` | 调整失败代理的隔离时间 |

所有公开运行字段、默认值和注释见 [`.env.example`](.env.example)，最终读取逻辑见 [`src/amazon_crawler/config.py`](src/amazon_crawler/config.py)。

## 任务输入示例

以下命令中的 `B0XXXXXXXX` 是一个合成 ASIN 格式标记。执行真实任务前，必须替换为目标商品详情页 `/dp/` 后面的 10 位 ASIN；它本身不代表真实商品。

### 商品详情

```bash
amazon-crawler create B0XXXXXXXX \
  --kind product \
  --marketplace US \
  --postal-code 10001
```

也可以直接提交受支持的 Amazon 商品 URL。URL 必须使用 HTTPS，并属于已允许的 Amazon 站点域名。

### 实时商品观测

```bash
amazon-crawler create B0XXXXXXXX \
  --kind product_time \
  --marketplace US \
  --postal-code 10001 \
  --mode realtime
```

未提供 `add_date` 的 `product_time` 表示“立即产生一次新观测”，每次提交都会创建新任务。调用方重试同一次业务请求时，应提供稳定的 `--idempotency-key`。

### 关键词搜索

```bash
amazon-crawler create \
  --kind search \
  --input-json '{"keyword":"wireless mouse","market_id":"US","post_code":"10001","turn_page":1,"frequent":0}'
```

搜索、类目、榜单和商家任务使用结构化 JSON。完整字段契约见 [`skills/operate-amazon-crawler/references/input-contracts.md`](skills/operate-amazon-crawler/references/input-contracts.md)。

## 通过 HTTP API 调用

创建商品任务：

```bash
curl -X POST http://127.0.0.1:3000/api/v1/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "kind": "product",
    "inputs": ["B0XXXXXXXX"],
    "marketplace_id": "US",
    "postal_code": "10001",
    "execution_mode": "standard",
    "options": {"result_sinks": ["sqlite"]}
  }'
```

查询任务和结果：

```bash
JOB_ID='JOB_ID_FROM_POST_RESPONSE'
curl "http://127.0.0.1:3000/api/v1/jobs/${JOB_ID}"
curl "http://127.0.0.1:3000/api/v1/jobs/${JOB_ID}/results"
curl "http://127.0.0.1:3000/api/v1/jobs/${JOB_ID}/events"
```

`JOB_ID_FROM_POST_RESPONSE` 表示 POST `/jobs` 响应中的 `job.id`。

接口基路径为 `/api/v1`。完整路由、请求字段和安全边界见 [`docs/API.md`](docs/API.md)。API 不接受 Cookie、代理、任意请求头、SQL、解析器代码或非白名单 URL。

## 结果存储

`sqlite` 始终作为标准结果原本。创建任务时可以重复指定 `--result-sink`，让同一结果可靠投递到多个已配置目标：

```bash
amazon-crawler create B0XXXXXXXX \
  --kind product \
  --marketplace US \
  --result-sink sqlite \
  --result-sink jsonl
```

可用 sink：

| Sink | 用途 | 前置条件 |
|---|---|---|
| `sqlite` | 控制面和标准结果原本 | 始终启用 |
| `jsonl` | 本地增量数据流 | 默认可用 |

Redis 和 MySQL 连接器属于扩展存储能力，启用前需要完成连接配置、数据契约确认和外部写入授权。P0 快速启动默认只使用 `sqlite` 和 `jsonl`。

抓取状态与投递状态彼此独立。Job 成功后，二级存储仍可能处于 `pending`、重试或 `dead_letter`，但不会影响 SQLite 中已经提交的标准结果。

## API 与 Worker 分离部署

本地开发可使用单进程 `serve`。需要独立伸缩时，将 API、抓取 Worker 分开运行，并共享同一个持久化数据库路径：

```bash
CRAWLER_WORKER_ENABLED=false \
CRAWLER_DELIVERY_WORKER_ENABLED=false \
amazon-crawler serve --host 0.0.0.0 --port 3000
```

另开进程：

```bash
amazon-crawler worker
```

当前存储使用 SQLite，适合单机多进程和 P0 验证，不适合多节点共享文件系统。未来扩展到多节点时，需要将任务状态、租约、幂等键和 outbox 一并切换到支持跨节点事务与锁语义的数据库，不能只更新结果表。

## Cookie 池与 Cookie 生产

Cookie 获取已经是管理界面中的独立功能。它会建立新的 Amazon 匿名会话、根据站点选择预设配送区域、回读页面确认地址生效，并将验证通过的 Cookie 写入 Redis 池。页面只接收资源池、站点和目标容量，不接收或显示 Cookie、代理凭证、提取链接及 Redis URL。

该入口默认关闭。先通过本机 `.env` 或部署平台 Secret 管理配置 Redis 与代理，再显式开启运维入口：

```bash
CRAWLER_COOKIE_OPERATIONS_API_ENABLED=true
```

重启服务后，在首页的“Amazon Cookie 资源”区域选择资源池和站点。系统会自动展示该站点的配送区域；填写目标池容量并勾选外部操作授权确认即可。页面会返回：实际配送区域、本次新建数、拒绝数、当前可用数以及安全失败码，不返回 Cookie 值。单次目标容量上限为 50，同一个池同时只允许一个获取操作。

若页面显示“运维 API 已关闭”，说明上述开关尚未开启；若显示“未配置 Redis 池”，说明 `CRAWLER_COOKIE_REDIS_URL` 和 `CRAWLER_COOKIE_REDIS_OVERSEAS_URL` 均未形成可用的 Cookie 生产池。默认 `CRAWLER_COOKIE_HARVEST_REQUIRE_PROXY=true`，没有可用代理时会安全失败，不会悄悄改为直连。

命令行补池方式见[需要新 Cookie 时](#需要新-cookie-时)。普通服务启动不会自动生产 Cookie。为 `product_hw` 或 JP 链路补充 overseas 池时使用 `--pool overseas`；普通补池无需 `--postal-code`，多邮编受控验收才显式覆盖。Cookie 生产会向 Amazon 发起请求，并向配置的 Cookie Redis 写入带 TTL 的数据。

生产链会建立首页会话、设置配送地址、重新加载页面确认邮编，再补充 locale/currency。只有验证通过的 Cookie 才会写入池。更严格的 US/JP 双站点受控验收流程见 [`docs/CONTROLLED_ACCEPTANCE.md`](docs/CONTROLLED_ACCEPTANCE.md)。

Cookie 获取属于人工运维权限，不属于抓取任务输入，也没有加入仓库中的 AI Agent Skill。当前 HTTP 服务没有登录和租户授权，因此即使开启了该开关，也只能绑定本机或放在具备认证、授权、审计和限流的内部控制面之后，不能直接暴露到公网。

## 开发与测试

运行完整单元测试：

```bash
PYTHONPATH=src:. python -m unittest discover -s tests -v
```

常用静态和契约检查：

```bash
python -m compileall -q src
amazon-crawler capabilities
node --check src/amazon_crawler/interfaces/static/app.js
```

## 项目目录

```text
amazon-crawler-v2/
├── src/amazon_crawler/
│   ├── domain/          # Job、状态、端口和资源领域模型
│   ├── application/     # 服务、抓取 Worker、投递 Worker、Cookie 维护
│   ├── infra/           # SQLite、HTTP、Cookie/代理、证据和结果 sink
│   ├── plugins/         # Amazon 各任务插件与解析器
│   └── interfaces/      # CLI、HTTP API 和 Web 管理界面
├── skills/              # AI Agent Skill 及输入契约
├── contracts/           # 机器可读的数据与验收契约
├── docs/                # 架构、API 和运行文档
├── scripts/             # 审计、采集、回放和证据编译工具
├── tests/               # 单元、契约、恢复和安全边界测试
├── .env.example         # 环境变量清单，不包含真实秘密
└── pyproject.toml       # Python 包和依赖定义
```

## 当前已知边界

- 当前 HTTP 服务没有登录、租户隔离、公网限流、用量配额和结果级授权，不能直接暴露到公网。
- 当前标准状态库是 SQLite；多节点分布式部署尚未实现。
- 真实站点访问仍需持续验证不同国家、邮编、页面形态和限流场景，离线测试不能替代线上验收。
- Cookie 生产代码已通过隔离测试；真实新 Cookie 的成功率仍受代理可用性和 Amazon 风控影响，不能把代码完成等同于生产资源可用。
- 商业化前需要完成目标站点条款、数据合规、访问频率和数据使用范围审查。

## 安全要求

- 不要提交 `.env`、Cookie、代理凭据、数据库 URL、Redis URL、原始响应或包含个人信息的数据。
- 对外运行前必须增加认证、租户授权、审计日志、速率限制、配额和结果访问控制。
- 外部 Cookie、Redis/MySQL 和真实站点采集都需要明确授权；本地测试通过不代表已获生产权限。
- 证据和事件只应保存经过白名单过滤及脱敏的字段。

## 进一步阅读

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)：分层、状态机、断点、资源和多存储设计
- [`docs/API.md`](docs/API.md)：HTTP API v1

## 许可证

本仓库当前未声明开源许可证。未经仓库所有者许可，不应复制、再分发或将代码用于其他商业项目。
